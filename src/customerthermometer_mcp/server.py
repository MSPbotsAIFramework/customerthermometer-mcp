import contextvars
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .api_client import CustomerThermometerClient
from .config import Settings

# Per-request credential isolation via contextvars.
# GatewayTokenMiddleware sets this before the MCP handler runs.
# Python asyncio copies context per task, so concurrent SSE connections are isolated.
_gateway_creds_var: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "customerthermometer_gateway_creds", default=None
)


def get_client_from_context(settings: Settings) -> CustomerThermometerClient | None:
    """Resolve the active CustomerThermometerClient for the current request context."""
    creds = _gateway_creds_var.get()
    if not creds:
        return None
    api_key, api_url = creds
    return CustomerThermometerClient(api_key, api_url)


class GatewayTokenMiddleware:
    """ASGI middleware.

    Reads X-CustomerThermometer-Api-Key and X-CustomerThermometer-Api-Url
    (both required) from request headers and stores them in the contextvar.
    Returns 401 if either is missing on /mcp requests.
    """

    def __init__(self, app: ASGIApp, settings: Settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not path.startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        api_key = request.headers.get("x-customerthermometer-api-key")
        api_url = request.headers.get("x-customerthermometer-api-url")
        if not api_key or not api_url:
            response = JSONResponse(
                {
                    "error": "Missing credentials",
                    "message": (
                        "This server requires the X-CustomerThermometer-Api-Key and "
                        "X-CustomerThermometer-Api-Url headers"
                    ),
                    "required_headers": [
                        "X-CustomerThermometer-Api-Key",
                        "X-CustomerThermometer-Api-Url",
                    ],
                    "optional_headers": [],
                },
                status_code=401,
            )
            await response(scope, receive, send)
            return

        ctx_token = _gateway_creds_var.set((api_key, api_url))
        try:
            await self.app(scope, receive, send)
        finally:
            _gateway_creds_var.reset(ctx_token)


def create_mcp_server(settings: Settings) -> FastMCP:
    """Build the FastMCP server instance and register all Customer Thermometer tools."""
    # DNS-rebinding protection is a browser-oriented safeguard that rejects
    # non-localhost Host headers with 421. Disable it so the server works
    # correctly behind a reverse proxy or docker network.
    mcp = FastMCP(
        name="customerthermometer-mcp",
        instructions=(
            "Customer Thermometer is an email-based CSAT/NPS survey platform: MSPs "
            'send 4-point "thermometer" emoji surveys (gold/green/yellow/red) or NPS '
            "surveys to customers and track responses. Core concepts: a Thermometer "
            "(survey template), a Blast (one send of a Thermometer to a recipient "
            "list), a Recipient List (contacts), and a Response (one rating + "
            "optional comment). Reporting tools: customerthermometer_get_thermometers "
            "and customerthermometer_get_recipient_lists look up template/list IDs; "
            "customerthermometer_get_send_quota checks remaining send credits; the "
            "metric tools (get_happiness_value, get_nps_value, get_temp_rating_value, "
            "get_response_rate_value, get_num_responses_value) return a single "
            "account-wide or blast-scoped score; get_blast_results and get_comments "
            "return the underlying response/comment records. All reporting tools "
            "accept an optional blast_id and/or date range to scope results. Action "
            "tools: customerthermometer_send_email sends a survey to one recipient; "
            "customerthermometer_add_recipient_to_list adds a contact to a list; "
            "customerthermometer_log_response manually records a response; "
            "customerthermometer_unsubscribe_recipient blocks future sends to an "
            "address; customerthermometer_delete_response removes one response "
            "(30-day recovery window). Typical flow: look up a thermometer/list ID, "
            "send or check quota, then use the metric/detail tools scoped to that "
            "blast_id to review results."
        ),
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        stateless_http=True,
        json_response=True,
    )

    client_factory: Callable[[], CustomerThermometerClient | None] = (
        lambda: get_client_from_context(settings)
    )

    from .tools import actions, reports

    reports.register(mcp, client_factory)
    actions.register(mcp, client_factory)

    return mcp
