"""Explicit live S3 preflight using synthetic objects only and a private journal."""

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from urllib.parse import urlsplit
from uuid import uuid4

from scripts.private_enrollment import DIRECTORY_FLAGS, PrivateArgumentParser, check_parent
from scripts.scaleway_backup_policy import ROOT, validate

SDK_VERSION = "1.43.123"
ENDPOINT = "https://s3.fr-par.scw.cloud"
V1, V2 = b"HAVEN_SYNTHETIC_PREFLIGHT_V1", b"HAVEN_SYNTHETIC_PREFLIGHT_V2"


class PreflightError(Exception):
    pass


def require(value):
    if not value:
        raise PreflightError()


def private_keys(directory):
    directory = Path(directory)
    require(directory.is_absolute() and ".." not in directory.parts)
    descriptor = os.open("/", DIRECTORY_FLAGS)
    try:
        check_parent(descriptor)
        for part in directory.parts[1:]:
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            check_parent(descriptor)
        info = os.fstat(descriptor)
        require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700)
        values = []
        for name in ("access-key", "secret-key"):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            try:
                info = os.fstat(fd)
                require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                        and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1 and info.st_size <= 129)
                value = os.read(fd, 130).decode("ascii")
                require(value.endswith("\n") and re.fullmatch(r"[A-Za-z0-9_+/=-]{8,128}", value[:-1]))
                values.append(value[:-1])
            finally:
                os.close(fd)
        require(values[0] != values[1])
        return values
    finally:
        os.close(descriptor)


def denied(error):
    response = getattr(error, "response", {})
    return (response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 403
            and response.get("Error", {}).get("Code") == "AccessDenied")


def diagnostic(error):
    # Never expose SDK messages, request URLs/headers, credential IDs or bodies.
    response = getattr(error, "response", {})
    code = response.get("Error", {}).get("Code")
    allowed = {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "RequestTimeTooSkewed",
               "NoSuchBucket", "AuthorizationHeaderMalformed", "InvalidToken"}
    return code if code in allowed else "preflight_failed"


class Journal:
    def __init__(self, path, settings, writes):
        self.path = Path(path)
        require(self.path.is_absolute() and ".." not in self.path.parts
                and self.path.parent.resolve(strict=True) == self.path.parent
                and not self.path.is_relative_to(ROOT))
        self.data = {"schema_version": 1, "sdk_version": SDK_VERSION,
                     "started_at_utc": datetime.now(timezone.utc).isoformat(),
                     "run_id": uuid4().hex, "bucket": settings["bucket"], "prefix": settings["prefix"],
                     "synthetic_writes": writes, "result": "running", "stage": "created",
                     "checks": {}, "operation_count": 0, "versions": [],
                     "not_tested": ["IAM_identity", "cross_bucket", "configuration_changes",
                                    "owner_version_recovery", "restic_remote_restore", "external_alerts"],
                     "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(self.data, stream, indent=2)

    def save(self):
        fd, temporary = tempfile.mkstemp(prefix=".haven-preflight-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(self.data, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)


class Probe:
    def __init__(self, client, anonymous, settings, journal):
        self.client, self.anonymous, self.journal = client, anonymous, journal
        self.bucket, self.prefix = settings["bucket"], settings["prefix"]
        run_id = journal.data["run_id"]
        self.object = f"{self.prefix}/preflight/{run_id}/payload"
        self.upload_key = f"{self.prefix}/preflight/{run_id}/unfinished"
        self.lock = f"{self.prefix}/locks/{run_id}{run_id}"
        journal.data["synthetic_objects"] = [self.object, self.upload_key, self.lock]
        self.lock_written = False

    def call(self, stage, operation, **kwargs):
        self.journal.data["stage"] = stage
        self.journal.data["operation_count"] += 1
        require(self.journal.data["operation_count"] <= 32)
        self.journal.save()  # Durable intent precedes every external operation.
        return operation(Bucket=self.bucket, **kwargs)

    def check(self, name, value):
        self.journal.data["checks"][name] = bool(value)
        self.journal.save()
        require(value)

    def refusal(self, name, operation, **kwargs):
        try:
            response = self.call(name, operation, **kwargs)
        except Exception as error:
            self.check(name, denied(error))
        else:
            body = response.get("Body")
            if body is not None:
                body.close()
            self.check(name, False)

    def read_only(self):
        r = self.call("bucket_location", self.client.get_bucket_location)
        self.check("region_matches", r.get("LocationConstraint") == "fr-par")
        r = self.call("bucket_versioning", self.client.get_bucket_versioning)
        self.check("versioning_enabled", r.get("Status") == "Enabled")
        r = self.call("prefix_listing", self.client.list_objects_v2, Prefix=self.prefix + "/", MaxKeys=1)
        self.check("prefix_listing_allowed", True)
        self.journal.data["repository_prefix_empty"] = not bool(r.get("Contents"))
        self.journal.save()
        self.refusal("outside_prefix_denied", self.client.list_objects_v2,
                     Prefix="haven-forbidden-preflight/", MaxKeys=1)
        self.refusal("unfiltered_listing_denied", self.client.list_objects_v2, MaxKeys=1)

    def synthetic(self):
        # Never adopt a nonempty repository or repeat an uncertain write run.
        self.check("empty_prefix_before_writes", self.journal.data.get("repository_prefix_empty") is True)
        for number, value in enumerate((V1, V2), 1):
            r = self.call(f"write_version_{number}", self.client.put_object,
                          Key=self.object, Body=value, StorageClass="STANDARD")
            version = r.get("VersionId")
            require(isinstance(version, str) and 0 < len(version) <= 1024 and version != "null")
            self.journal.data["versions"].append(version)
            self.journal.save()
        self.check("distinct_object_versions", len(set(self.journal.data["versions"])) == 2)
        r = self.call("read_latest", self.client.get_object, Key=self.object)
        body = r["Body"]
        try:
            self.check("latest_bytes_match", body.read(129) == V2)
        finally:
            body.close()
        self.refusal("anonymous_read_denied", self.anonymous.get_object, Key=self.object)
        self.refusal("version_read_denied", self.client.get_object, Key=self.object,
                     VersionId=self.journal.data["versions"][0])
        # These deletions target only this run's synthetic object/version. An
        # unexpected allow fails immediately; no real recovery point is touched.
        self.refusal("data_delete_denied", self.client.delete_object, Key=self.object)
        self.refusal("version_delete_denied", self.client.delete_object, Key=self.object,
                     VersionId=self.journal.data["versions"][0])
        r = self.call("create_multipart", self.client.create_multipart_upload,
                      Key=self.upload_key, StorageClass="STANDARD")
        upload = r.get("UploadId")
        require(isinstance(upload, str) and 0 < len(upload) <= 4096)
        self.journal.data["multipart_upload_id"] = upload
        self.journal.save()
        self.call("list_parts", self.client.list_parts, Key=self.upload_key, UploadId=upload, MaxParts=1)
        r = self.call("list_multipart", self.client.list_multipart_uploads,
                      Prefix=self.upload_key, MaxUploads=1)
        self.check("multipart_listing_allowed", any(x.get("UploadId") == upload for x in r.get("Uploads", [])))
        self.call("abort_multipart", self.client.abort_multipart_upload, Key=self.upload_key, UploadId=upload)
        self.journal.data["multipart_aborted"] = True
        self.journal.save()
        # Mark intent before PUT: a transport failure can have committed the lock.
        self.lock_written = True
        self.call("write_lock", self.client.put_object, Key=self.lock, Body=b"HAVEN_SYNTHETIC_LOCK")
        self.call("remove_lock", self.client.delete_object, Key=self.lock)
        self.lock_written = False
        self.verify_lock_removed()

    def verify_lock_removed(self):
        r = self.call("verify_lock_removed", self.client.list_objects_v2, Prefix=self.lock, MaxKeys=1)
        self.check("lock_cleanup_verified", not bool(r.get("Contents")))

    def run(self, writes=False):
        try:
            self.read_only()
            if writes:
                self.synthetic()
            self.journal.data.update(result="passed", stage="complete")
        except BaseException as error:
            self.journal.data.update(result="failed", diagnostic=diagnostic(error),
                                     failed_stage=self.journal.data["stage"])
            upload = self.journal.data.get("multipart_upload_id")
            if upload and not self.journal.data.get("multipart_aborted"):
                try:
                    self.call("failure_multipart_cleanup", self.client.abort_multipart_upload,
                              Key=self.upload_key, UploadId=upload)
                    self.journal.data["failure_multipart_cleanup_sent"] = True
                except Exception:
                    self.journal.data["failure_multipart_cleanup_sent"] = False
            # Never retry an uncertain PUT. Only remove this run's possible lock.
            if self.lock_written:
                try:
                    self.call("failure_lock_cleanup", self.client.delete_object, Key=self.lock)
                    self.journal.data["failure_lock_cleanup_sent"] = True
                except Exception:
                    self.journal.data["failure_lock_cleanup_sent"] = False
        finally:
            self.journal.data["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
            self.journal.save()
        return self.journal.data["result"] == "passed"


def clients(settings, keys, writes):
    import botocore
    import botocore.session
    from botocore.config import Config
    require(botocore.__version__ == SDK_VERSION)
    session = botocore.session.get_session()
    session.set_config_variable("config_file", "/nonexistent/haven-config")
    session.set_config_variable("credentials_file", "/nonexistent/haven-credentials")
    attempts = 0

    def guard(request, **kwargs):
        nonlocal attempts
        attempts += 1
        url = urlsplit(request.url)
        base = "/" + settings["bucket"]
        require(attempts <= 40 and url.scheme == "https" and url.hostname == "s3.fr-par.scw.cloud"
                and (url.path == base or url.path.startswith(base + "/")))
        if request.method not in ("GET", "HEAD"):
            require(writes and url.path.startswith(base + "/" + settings["prefix"] + "/")
                    and request.method in ("PUT", "POST", "DELETE"))

    def create(unsigned=False):
        config = Config(signature_version=botocore.UNSIGNED if unsigned else "s3v4",
                        s3={"addressing_style": "path"}, proxies={}, connect_timeout=5, read_timeout=10,
                        retries={"total_max_attempts": 1, "mode": "standard"},
                        ignore_configured_endpoint_urls=True,
                        request_checksum_calculation="when_required", response_checksum_validation="when_required")
        client = session.create_client("s3", region_name="fr-par", endpoint_url=ENDPOINT,
                                       aws_access_key_id="unsigned" if unsigned else keys[0],
                                       aws_secret_access_key="unsigned" if unsigned else keys[1],
                                       verify=True, config=config)
        client.meta.events.register("before-send.s3", guard)
        return client
    return create(), create(unsigned=True)


def main(argv=None):
    parser = PrivateArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--synthetic-writes", action="store_true")
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.live:
        print("Explicit --live is required; no credentials read or request sent.", file=sys.stderr)
        return 2
    logging.disable(logging.CRITICAL)
    journal = None
    try:
        require(not args.settings.is_symlink() and args.settings.stat().st_size <= 16384)
        settings = json.loads(args.settings.read_text())
        validate(settings)
        journal = Journal(args.receipt, settings, args.synthetic_writes)
        client, anonymous = clients(settings, private_keys(args.credentials), args.synthetic_writes)
        probe = Probe(client, anonymous, settings, journal)
        passed = probe.run(writes=args.synthetic_writes)
    except BaseException as error:
        if journal is not None:
            journal.data.update(result="failed", diagnostic=diagnostic(error),
                                finished_at_utc=datetime.now(timezone.utc).isoformat())
            try:
                journal.save()
            except OSError:
                pass
        print(json.dumps({"result": "failed", "diagnostic": diagnostic(error), "values_disclosed": False}))
        return 1
    print(json.dumps({"result": "passed" if passed else "failed",
                      "stage": journal.data.get("failed_stage", journal.data["stage"]),
                      "checks": journal.data["checks"], "operation_count": journal.data["operation_count"],
                      "values_disclosed": False}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
