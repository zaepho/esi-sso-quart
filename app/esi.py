import asyncio
import dataclasses
import datetime
import http
import inspect
import contextlib
import logging
import json
import typing

import aiohttp
import aiohttp.client_exceptions
import dateutil.parser
import redis.asyncio
import yarl

from support.telemetry import otel, otel_add_error

from .constants import AppConstants


@dataclasses.dataclass(frozen=True)
class AppESIResult:
    status: http.HTTPStatus
    data: list | dict = dataclasses.field(default=None)


class AppESI:

    IF_NON_MATCH: typing.Final = 'If-None-Match'
    ETAG: typing.Final = 'ETag'
    EXPIRES: typing.Final = 'Expires'
    PAGES: typing.Final = 'X-Pages'
    KV_LIFETIME: typing.Final = 3600

    SELF: typing.ClassVar = None

    @classmethod
    def factory(cls, logger: logging.Logger):
        if cls.SELF is None:
            redis_pool: typing.Final = redis.asyncio.ConnectionPool.from_url("redis://localhost/1", decode_responses=True)
            cls.SELF = cls(redis_pool, logger)
        return cls.SELF

    def __init__(self, redis_pool: redis.asyncio.ConnectionPool, logger: logging.Logger) -> None:
        self.redis_pool: typing.Final = redis_pool
        self.logger: typing.Final = logger

    async def url(self, url: str, params: dict = None) -> yarl.URL:
        u = yarl.URL(url)
        if params:
            return u.update_query(params)
        return u

    @otel
    async def get(self, http_session: aiohttp.ClientSession, url: str, request_headers: dict = None, request_params: dict = None) -> AppESIResult:

        request_headers = request_headers or dict()

        redis_url: typing.Final = await self.url(url, request_params)
        etag_key: typing.Final = f"etag:{redis_url!s}"
        json_key: typing.Final = f"json:{redis_url!s}"

        previous_json: dict = None
        with contextlib.suppress(redis.RedisError):
            async with redis.asyncio.Redis.from_pool(self.redis_pool) as rc:
                pv: typing.Final = await rc.get(json_key)
                if pv:
                    previous_json = json.loads(pv)

        previous_etag: str = None
        with contextlib.suppress(redis.RedisError):
            async with redis.asyncio.Redis.from_pool(self.redis_pool) as rc:
                previous_etag = await rc.get(etag_key)
                if previous_etag is not None and previous_json is not None:
                    request_headers[self.IF_NON_MATCH] = previous_etag

        result_status = http.HTTPStatus.NOT_FOUND
        result_data = None

        attempts_remaining = AppConstants.ESI_ERROR_RETRY_COUNT
        while result_data is None and attempts_remaining > 0:
            try:
                async with await http_session.get(url, headers=request_headers, params=request_params) as response:
                    result_status = response.status

                    if response.status in [http.HTTPStatus.OK]:
                        response_json: typing.Final = await response.json()
                        response_etag: typing.Final = response.headers.get(self.ETAG)
                        response_expires: typing.Final = response.headers.get(self.EXPIRES)
                        result_data = response_json
                        if response_etag is not None and response_json is not None:
                            lifetime = self.KV_LIFETIME
                            if response_expires:
                                lifetime = dateutil.parser.parse(response_expires) - datetime.datetime.now(tz=datetime.UTC)
                                lifetime = int(lifetime.total_seconds())

                            with contextlib.suppress(redis.RedisError):
                                async with redis.asyncio.Redis.from_pool(self.redis_pool) as rc:
                                    tasks: typing.Final = list()
                                    tasks.append(rc.setex(name=json_key, value=json.dumps(response_json), time=lifetime))
                                    tasks.append(rc.setex(name=etag_key, value=response_etag, time=lifetime))
                                    await asyncio.gather(*tasks)
                        break

                    if response.status in [http.HTTPStatus.NOT_MODIFIED]:
                        result_data = previous_json
                        break

                    attempts_remaining -= 1
                    otel_add_error(f"{response.url} -> {response.status}")
                    self.logger.warning(f"- {self.__class__.__name__}.{inspect.currentframe().f_code.co_name}: {response.url} -> {response.status} / {await response.text()}")
                    if response.status in [http.HTTPStatus.BAD_REQUEST, http.HTTPStatus.FORBIDDEN]:
                        attempts_remaining = 0
                    if attempts_remaining > 0:
                        await asyncio.sleep(AppConstants.ESI_ERROR_SLEEP_TIME * AppConstants.ESI_ERROR_SLEEP_MODIFIERS.get(response.status, 1))

            except aiohttp.client_exceptions.ClientConnectionError as ex:
                attempts_remaining -= 1
                otel_add_error(f"{url} -> {ex=}")
                self.logger.error(f"- {self.__class__.__name__}.{inspect.currentframe().f_code.co_name}: {url=}, {ex=}")
                if attempts_remaining > 0:
                    await asyncio.sleep(AppConstants.ESI_ERROR_SLEEP_TIME)

            except Exception as ex:
                otel_add_error(f"{url} -> {ex=}")
                self.logger.error(f"- {self.__class__.__name__}.{inspect.currentframe().f_code.co_name}: {url=}, {ex=}")
                break

        self.logger.info(f"- {self.__class__.__name__}.{inspect.currentframe().f_code.co_name}: {response.url} -> {result_status}")
        return AppESIResult(result_status, result_data)

    @otel
    async def post(self, http_session: aiohttp.ClientSession, url: str, body: dict, request_headers: dict = None, request_params: dict = None) -> AppESIResult:

        result_status = http.HTTPStatus.NOT_FOUND
        result_data = None

        attempts_remaining = AppConstants.ESI_ERROR_RETRY_COUNT
        while result_data is None and attempts_remaining > 0:
            try:
                async with await http_session.post(url, headers=request_headers, data=body, params=request_params) as response:
                    result_status = response.status

                    if response.status in [http.HTTPStatus.OK]:
                        result_data = await response.json()
                        break

                    attempts_remaining -= 1
                    otel_add_error(f"{response.url} -> {response.status}")
                    self.logger.warning(f"- {self.__class__.__name__}.{inspect.currentframe().f_code.co_name}: {response.url} -> {response.status} / {await response.text()}")
                    if response.status in [http.HTTPStatus.BAD_REQUEST, http.HTTPStatus.FORBIDDEN, http.HTTPStatus.UNAUTHORIZED]:
                        attempts_remaining = 0
                    if attempts_remaining > 0:
                        await asyncio.sleep(AppConstants.ESI_ERROR_SLEEP_TIME * AppConstants.ESI_ERROR_SLEEP_MODIFIERS.get(response.status, 1))

            except aiohttp.client_exceptions.ClientConnectionError as ex:
                attempts_remaining -= 1
                otel_add_error(f"{url} -> {ex=}")
                self.logger.error(f"- {self.__class__.__name__}.{inspect.currentframe().f_code.co_name}: {url=}, {ex=}")
                if attempts_remaining > 0:
                    await asyncio.sleep(AppConstants.ESI_ERROR_SLEEP_TIME)

            except Exception as ex:
                otel_add_error(f"{url} -> {ex=}")
                self.logger.error(f"- {self.__class__.__name__}.{inspect.currentframe().f_code.co_name}: {url=}, {ex=}")
                break

        return AppESIResult(result_status, result_data)

    async def status(self) -> bool:
        async with aiohttp.ClientSession() as http_session:
            request_params: typing.Final = {
                "datasource": "tranquility",
                "language": "en"
            }
            status_result = await self.get(http_session, f"{AppConstants.ESI_API_ROOT}{AppConstants.ESI_API_VERSION}/status/", request_params=request_params)
            self.logger.info(f"{self.__class__.__name__}.{inspect.currentframe().f_code.co_name}: {status_result=}")
            if status_result.status in [http.HTTPStatus.OK, http.HTTPStatus.NOT_MODIFIED] and status_result.data is not None:
                if all([int(status_result.data.get("players", 0)) > 128, bool(status_result.data.get("vip", False)) is False]):
                    return True

        return False

    async def pages(self, url: str, access_token: str, request_params: dict = None) -> AppESIResult:

        session_headers: typing.Final = dict()
        if len(access_token) > 0:
            session_headers["Authorization"] = f"Bearer {access_token}"

        request_headers = dict()

        redis_url: typing.Final = await self.url(url, request_params)
        etag_key: typing.Final = f"etag:{redis_url!s}"
        pages_key: typing.Final = f"pages:{redis_url!s}"
        json_key: typing.Final = f"json:{redis_url!s}"

        previous_json: dict = None
        with contextlib.suppress(redis.RedisError):
            async with redis.asyncio.Redis.from_pool(self.redis_pool) as rc:
                pv = await rc.get(json_key)
                if pv is not None:
                    previous_json = json.loads(pv)

        previous_pages: int = 0
        with contextlib.suppress(redis.RedisError):
            async with redis.asyncio.Redis.from_pool(self.redis_pool) as rc:
                pv = await rc.get(pages_key)
                if pv is not None:
                    previous_pages = int(pv)

        previous_etag: str = None
        with contextlib.suppress(redis.RedisError):
            async with redis.asyncio.Redis.from_pool(self.redis_pool) as rc:
                previous_etag = await rc.get(etag_key)
                if previous_etag is not None and previous_json is not None and previous_pages > 0:
                    request_headers[AppESI.IF_NON_MATCH] = previous_etag

        result_status = http.HTTPStatus.NOT_FOUND
        result_data = None
        maxpageno: int = 0

        async with aiohttp.ClientSession(headers=session_headers, connector=aiohttp.TCPConnector(limit_per_host=AppConstants.ESI_LIMIT_PER_HOST)) as http_session:

            attempts_remaining = AppConstants.ESI_ERROR_RETRY_COUNT
            while result_data is None and attempts_remaining > 0:
                try:
                    async with await http_session.get(url, headers=request_headers, params=request_params) as response:
                        result_status = response.status

                        if response.status in [http.HTTPStatus.OK]:
                            response_pages = int(response.headers.get(AppESI.PAGES, 1))
                            response_json: typing.Final = await response.json()
                            response_etag: typing.Final = response.headers.get(AppESI.ETAG)
                            response_expires: typing.Final = response.headers.get(AppESI.EXPIRES)

                            if response_etag is not None and response_json is not None:
                                lifetime = AppESI.KV_LIFETIME
                                if response_expires:
                                    lifetime = dateutil.parser.parse(response_expires) - datetime.datetime.now(tz=datetime.UTC)
                                    lifetime = int(lifetime.total_seconds())

                                with contextlib.suppress(redis.RedisError):
                                    async with redis.asyncio.Redis.from_pool(self.redis_pool) as rc:
                                        tasks: typing.Final = list()
                                        tasks.append(rc.setex(name=json_key, value=json.dumps(response_json), time=lifetime))
                                        tasks.append(rc.setex(name=pages_key, value=response_pages, time=lifetime))
                                        tasks.append(rc.setex(name=etag_key, value=response_etag, time=lifetime))
                                        await asyncio.gather(*tasks)

                            maxpageno = response_pages
                            result_data = list()
                            result_data.extend(response_json)
                            break

                        elif response.status in [http.HTTPStatus.NOT_MODIFIED]:
                            maxpageno = previous_pages
                            result_data = list()
                            result_data.extend(previous_json)
                            break

                        else:
                            attempts_remaining -= 1
                            otel_add_error(f"{response.url} -> {response.status}")
                            self.logger.warning("- {}.{}: {}".format(self.__class__.__name__, inspect.currentframe().f_code.co_name, f"{response.url} -> {response.status}"))
                            if response.status in [http.HTTPStatus.BAD_REQUEST, http.HTTPStatus.FORBIDDEN]:
                                attempts_remaining = 0
                            if attempts_remaining > 0:
                                await asyncio.sleep(AppConstants.ESI_ERROR_SLEEP_TIME * AppConstants.ESI_ERROR_SLEEP_MODIFIERS.get(response.status, 1))

                except aiohttp.ClientConnectionError as ex:
                    attempts_remaining -= 1
                    otel_add_error(f"{url} -> {ex=}")
                    self.logger.error(f"- {self.__class__.__name__}.{inspect.currentframe().f_code.co_name}: {url=}, {ex=}")
                    if attempts_remaining > 0:
                        await asyncio.sleep(AppConstants.ESI_ERROR_SLEEP_TIME)

                except Exception as ex:
                    otel_add_error(f"{url} -> {ex=}")
                    self.logger.error(f"- {self.__class__.__name__}.{inspect.currentframe().f_code.co_name}: {url=}, {ex=}")
                    break

            if result_data is not None:
                pages = list(range(2, 1 + int(maxpageno)))
                task_list: typing.Final = [self.get(http_session, url, request_params=request_params | {"page": x}) for x in pages]
                if len(task_list) > 0:
                    for result in await asyncio.gather(*task_list):
                        if isinstance(result, AppESIResult):
                            if result.status in [http.HTTPStatus.OK, http.HTTPStatus.NOT_MODIFIED]:
                                result_data.extend(result.data)
                            else:
                                result_status = result.status
                                result_data = None
                                break

        self.logger.info(f"- {self.__class__.__name__}.{inspect.currentframe().f_code.co_name}: {redis_url} -> {result_status}")
        return AppESIResult(result_status, result_data)
