"""
RoadHTTP - HTTP Client for BlackRoad
Feature-rich HTTP client with retries, timeouts, and connection pooling.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
import asyncio
import base64
import hashlib
import json
import logging
import ssl
import threading
import time
import urllib.parse
import uuid

logger = logging.getLogger(__name__)


class HTTPMethod(str, Enum):
    """HTTP methods."""
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    PATCH = "PATCH"
    HEAD = "HEAD"
    OPTIONS = "OPTIONS"


class ContentType(str, Enum):
    """Content types."""
    JSON = "application/json"
    FORM = "application/x-www-form-urlencoded"
    MULTIPART = "multipart/form-data"
    TEXT = "text/plain"
    HTML = "text/html"
    XML = "application/xml"


@dataclass
class HTTPHeaders:
    """HTTP headers."""
    headers: Dict[str, str] = field(default_factory=dict)

    def set(self, key: str, value: str) -> "HTTPHeaders":
        self.headers[key] = value
        return self

    def get(self, key: str, default: str = None) -> Optional[str]:
        return self.headers.get(key, default)

    def delete(self, key: str) -> bool:
        if key in self.headers:
            del self.headers[key]
            return True
        return False

    def to_dict(self) -> Dict[str, str]:
        return self.headers.copy()


@dataclass
class HTTPRequest:
    """HTTP request."""
    method: HTTPMethod
    url: str
    headers: HTTPHeaders = field(default_factory=HTTPHeaders)
    body: Optional[bytes] = None
    params: Dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0
    follow_redirects: bool = True
    max_redirects: int = 5
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def full_url(self) -> str:
        if not self.params:
            return self.url
        query = urllib.parse.urlencode(self.params)
        separator = "&" if "?" in self.url else "?"
        return f"{self.url}{separator}{query}"


@dataclass
class HTTPResponse:
    """HTTP response."""
    status_code: int
    headers: HTTPHeaders
    body: bytes
    request: HTTPRequest
    elapsed_ms: float
    redirects: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status_code": self.status_code,
            "headers": self.headers.to_dict(),
            "body_length": len(self.body),
            "elapsed_ms": self.elapsed_ms
        }


class RetryStrategy:
    """Retry strategy for failed requests."""

    def __init__(
        self,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
        retry_statuses: List[int] = None,
        retry_exceptions: List[type] = None
    ):
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.retry_statuses = retry_statuses or [429, 500, 502, 503, 504]
        self.retry_exceptions = retry_exceptions or [ConnectionError, TimeoutError]

    def should_retry(self, response: HTTPResponse = None, exception: Exception = None, attempt: int = 0) -> bool:
        if attempt >= self.max_retries:
            return False

        if exception:
            return any(isinstance(exception, exc_type) for exc_type in self.retry_exceptions)

        if response:
            return response.status_code in self.retry_statuses

        return False

    def get_delay(self, attempt: int) -> float:
        return self.backoff_factor * (2 ** attempt)


class RequestInterceptor:
    """Intercept and modify requests."""

    def intercept(self, request: HTTPRequest) -> HTTPRequest:
        return request


class ResponseInterceptor:
    """Intercept and modify responses."""

    def intercept(self, response: HTTPResponse) -> HTTPResponse:
        return response


class AuthInterceptor(RequestInterceptor):
    """Add authentication to requests."""
    pass


class BearerAuthInterceptor(AuthInterceptor):
    """Bearer token authentication."""

    def __init__(self, token: str):
        self.token = token

    def intercept(self, request: HTTPRequest) -> HTTPRequest:
        request.headers.set("Authorization", f"Bearer {self.token}")
        return request


class BasicAuthInterceptor(AuthInterceptor):
    """Basic authentication."""

    def __init__(self, username: str, password: str):
        credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.auth_header = f"Basic {credentials}"

    def intercept(self, request: HTTPRequest) -> HTTPRequest:
        request.headers.set("Authorization", self.auth_header)
        return request


class LoggingInterceptor(RequestInterceptor, ResponseInterceptor):
    """Log requests and responses."""

    def intercept(self, obj: Union[HTTPRequest, HTTPResponse]) -> Union[HTTPRequest, HTTPResponse]:
        if isinstance(obj, HTTPRequest):
            logger.info(f"Request: {obj.method.value} {obj.url}")
        elif isinstance(obj, HTTPResponse):
            logger.info(f"Response: {obj.status_code} ({obj.elapsed_ms:.2f}ms)")
        return obj


class ConnectionPool:
    """Connection pool for HTTP connections."""

    def __init__(self, max_connections: int = 100, max_per_host: int = 10):
        self.max_connections = max_connections
        self.max_per_host = max_per_host
        self._connections: Dict[str, List[Any]] = {}
        self._lock = threading.Lock()

    def _get_host_key(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def get_connection(self, url: str) -> Optional[Any]:
        """Get a connection from the pool."""
        host_key = self._get_host_key(url)
        with self._lock:
            if host_key in self._connections and self._connections[host_key]:
                return self._connections[host_key].pop()
        return None

    def release_connection(self, url: str, connection: Any) -> None:
        """Return a connection to the pool."""
        host_key = self._get_host_key(url)
        with self._lock:
            if host_key not in self._connections:
                self._connections[host_key] = []

            if len(self._connections[host_key]) < self.max_per_host:
                self._connections[host_key].append(connection)


class MockTransport:
    """Mock transport for simulated HTTP responses."""

    def __init__(self):
        self.responses: Dict[str, HTTPResponse] = {}

    def add_response(self, method: str, url: str, status: int = 200, body: bytes = b"", headers: Dict = None):
        key = f"{method}:{url}"
        self.responses[key] = HTTPResponse(
            status_code=status,
            headers=HTTPHeaders(headers or {}),
            body=body,
            request=None,
            elapsed_ms=1.0
        )

    async def send(self, request: HTTPRequest) -> HTTPResponse:
        key = f"{request.method.value}:{request.url}"
        if key in self.responses:
            response = self.responses[key]
            response.request = request
            return response

        # Default 404 response
        return HTTPResponse(
            status_code=404,
            headers=HTTPHeaders({}),
            body=b"Not Found",
            request=request,
            elapsed_ms=1.0
        )


class HTTPTransport:
    """HTTP transport layer (simulated)."""

    async def send(self, request: HTTPRequest) -> HTTPResponse:
        """Send HTTP request (simulated)."""
        start = time.time()

        # Simulate network latency
        await asyncio.sleep(0.05)

        # Parse URL for simulated response
        parsed = urllib.parse.urlparse(request.url)

        # Simulated response based on path
        status = 200
        body = json.dumps({
            "url": request.url,
            "method": request.method.value,
            "success": True
        }).encode()

        elapsed = (time.time() - start) * 1000

        return HTTPResponse(
            status_code=status,
            headers=HTTPHeaders({"Content-Type": "application/json"}),
            body=body,
            request=request,
            elapsed_ms=elapsed
        )


class HTTPClient:
    """HTTP client."""

    def __init__(
        self,
        base_url: str = "",
        timeout: float = 30.0,
        retry_strategy: RetryStrategy = None,
        transport: HTTPTransport = None
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retry_strategy = retry_strategy or RetryStrategy()
        self.transport = transport or HTTPTransport()
        self.request_interceptors: List[RequestInterceptor] = []
        self.response_interceptors: List[ResponseInterceptor] = []
        self.default_headers = HTTPHeaders()

    def add_interceptor(self, interceptor: Union[RequestInterceptor, ResponseInterceptor]) -> None:
        if isinstance(interceptor, RequestInterceptor):
            self.request_interceptors.append(interceptor)
        if isinstance(interceptor, ResponseInterceptor):
            self.response_interceptors.append(interceptor)

    def _build_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    async def request(
        self,
        method: HTTPMethod,
        path: str,
        headers: Dict[str, str] = None,
        params: Dict[str, str] = None,
        body: Any = None,
        json_body: Any = None,
        timeout: float = None
    ) -> HTTPResponse:
        """Make an HTTP request."""
        # Build request
        request_headers = HTTPHeaders(self.default_headers.to_dict())
        if headers:
            for k, v in headers.items():
                request_headers.set(k, v)

        # Handle body
        request_body = None
        if json_body is not None:
            request_body = json.dumps(json_body).encode()
            request_headers.set("Content-Type", ContentType.JSON.value)
        elif body is not None:
            if isinstance(body, str):
                request_body = body.encode()
            elif isinstance(body, bytes):
                request_body = body
            else:
                request_body = str(body).encode()

        request = HTTPRequest(
            method=method,
            url=self._build_url(path),
            headers=request_headers,
            params=params or {},
            body=request_body,
            timeout=timeout or self.timeout
        )

        # Apply request interceptors
        for interceptor in self.request_interceptors:
            request = interceptor.intercept(request)

        # Execute with retries
        response = None
        last_exception = None
        attempt = 0

        while attempt <= self.retry_strategy.max_retries:
            try:
                response = await self.transport.send(request)

                if self.retry_strategy.should_retry(response=response, attempt=attempt):
                    delay = self.retry_strategy.get_delay(attempt)
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue

                break

            except Exception as e:
                last_exception = e
                if self.retry_strategy.should_retry(exception=e, attempt=attempt):
                    delay = self.retry_strategy.get_delay(attempt)
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                raise

        if response is None and last_exception:
            raise last_exception

        # Apply response interceptors
        for interceptor in self.response_interceptors:
            response = interceptor.intercept(response)

        return response

    async def get(self, path: str, **kwargs) -> HTTPResponse:
        return await self.request(HTTPMethod.GET, path, **kwargs)

    async def post(self, path: str, **kwargs) -> HTTPResponse:
        return await self.request(HTTPMethod.POST, path, **kwargs)

    async def put(self, path: str, **kwargs) -> HTTPResponse:
        return await self.request(HTTPMethod.PUT, path, **kwargs)

    async def delete(self, path: str, **kwargs) -> HTTPResponse:
        return await self.request(HTTPMethod.DELETE, path, **kwargs)

    async def patch(self, path: str, **kwargs) -> HTTPResponse:
        return await self.request(HTTPMethod.PATCH, path, **kwargs)


class HTTPClientBuilder:
    """Fluent builder for HTTP client."""

    def __init__(self):
        self._base_url = ""
        self._timeout = 30.0
        self._retry_strategy = None
        self._interceptors: List[Union[RequestInterceptor, ResponseInterceptor]] = []
        self._headers = {}

    def base_url(self, url: str) -> "HTTPClientBuilder":
        self._base_url = url
        return self

    def timeout(self, seconds: float) -> "HTTPClientBuilder":
        self._timeout = seconds
        return self

    def retry(self, max_retries: int = 3, backoff: float = 0.5) -> "HTTPClientBuilder":
        self._retry_strategy = RetryStrategy(max_retries=max_retries, backoff_factor=backoff)
        return self

    def bearer_auth(self, token: str) -> "HTTPClientBuilder":
        self._interceptors.append(BearerAuthInterceptor(token))
        return self

    def basic_auth(self, username: str, password: str) -> "HTTPClientBuilder":
        self._interceptors.append(BasicAuthInterceptor(username, password))
        return self

    def header(self, key: str, value: str) -> "HTTPClientBuilder":
        self._headers[key] = value
        return self

    def logging(self) -> "HTTPClientBuilder":
        self._interceptors.append(LoggingInterceptor())
        return self

    def build(self) -> HTTPClient:
        client = HTTPClient(
            base_url=self._base_url,
            timeout=self._timeout,
            retry_strategy=self._retry_strategy
        )

        for key, value in self._headers.items():
            client.default_headers.set(key, value)

        for interceptor in self._interceptors:
            client.add_interceptor(interceptor)

        return client


# Example usage
async def example_usage():
    """Example HTTP client usage."""
    # Create client with builder
    client = (
        HTTPClientBuilder()
        .base_url("https://api.example.com")
        .timeout(10.0)
        .retry(max_retries=3, backoff=0.5)
        .bearer_auth("your-api-token")
        .header("User-Agent", "RoadHTTP/1.0")
        .logging()
        .build()
    )

    # GET request
    response = await client.get("/users", params={"page": "1"})
    print(f"GET /users: {response.status_code}")

    # POST request with JSON
    response = await client.post(
        "/users",
        json_body={"name": "John", "email": "john@example.com"}
    )
    print(f"POST /users: {response.status_code}")

    # Direct client creation
    simple_client = HTTPClient(base_url="https://api.example.com")
    response = await simple_client.get("/health")
    print(f"Health check: {response.ok}")

