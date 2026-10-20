"""应用共享异步 Redis client 构造"""

from redis.asyncio import Redis

from tinkerfin_studio.config.settings import RedisConnectionSettings


def create_redis_client(
    settings: RedisConnectionSettings, *, database: int | None = None
) -> Redis:
    """创建由应用生命周期拥有的二进制 Redis client"""

    return Redis(
        host=settings.host,
        port=settings.port,
        password=(
            None if settings.password is None else settings.password.get_secret_value()
        ),
        db=settings.database if database is None else database,
        decode_responses=False,
        max_connections=settings.max_connections,
        socket_connect_timeout=settings.socket_timeout_seconds,
        socket_timeout=settings.socket_timeout_seconds,
        health_check_interval=30,
    )
