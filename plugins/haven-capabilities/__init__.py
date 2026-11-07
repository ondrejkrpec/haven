"""Native Hermes plugin. Execution policy is armed by the trusted host, not by hooks."""


def register(ctx):
    from pathlib import Path
    from hermes_constants import get_hermes_home
    from scripts.capability_policy import CapabilityService, normalized

    service = CapabilityService(Path(get_hermes_home()), owner=ctx.get_config("owner_id"))
    settings = ctx.get_config("wishlist")
    if settings is not None:
        from scripts.wishlist_ingress import Bridge
        bridge = Bridge(service.owner, settings)
        ctx.register_hook("pre_gateway_dispatch", bridge.capture)

        async def consume(**kwargs):
            response = await bridge.consume(**kwargs)
            if response is not None:
                return response
            response = service.consume(**kwargs)
            if (service.aliases.get(normalized(kwargs.get("text"))) == "system.capabilities"
                    and response["reply"].startswith("Můžeš se zeptat:")):
                response["reply"] += " · Wishlist: wl pomoc"
            return response

        ctx.register_hook("post_gateway_admission", consume)
    else:
        ctx.register_hook("post_gateway_admission", service.consume)
    ctx.register_hook("pre_tool_call", service.block_tool)
