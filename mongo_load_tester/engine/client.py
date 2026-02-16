"""Mongo client helpers."""

from __future__ import annotations

from pymongo import MongoClient

from mongo_load_tester.config import MongoConnectionConfig


def build_client(config: MongoConnectionConfig) -> MongoClient:
    """Create a tuned MongoClient for load generation."""

    return MongoClient(
        config.uri,
        appname=config.app_name,
        maxPoolSize=config.max_pool_size,
        minPoolSize=config.min_pool_size,
        connectTimeoutMS=config.connect_timeout_ms,
        socketTimeoutMS=config.socket_timeout_ms,
        serverSelectionTimeoutMS=config.server_selection_timeout_ms,
        retryWrites=config.retry_writes,
        tls=config.tls,
    )
