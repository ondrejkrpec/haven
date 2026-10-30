"""Native Hermes plugin. Execution policy is armed by the trusted host, not by hooks."""


def register(ctx):
    from pathlib import Path
    from hermes_constants import get_hermes_home
    from scripts.capability_policy import CapabilityService

    service = CapabilityService(Path(get_hermes_home()), owner=ctx.get_config("owner_id"))
    ctx.register_hook("post_gateway_admission", service.consume)
    ctx.register_hook("pre_tool_call", service.block_tool)
