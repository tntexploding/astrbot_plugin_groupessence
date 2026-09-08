from __future__ import annotations

import asyncio

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .src.group_essence_extractor.astrbot_source import (
    AstrBotEssenceSource,
    OneBotActionError,
)
from .src.group_essence_extractor.astrbot_gateway import AstrBotOneBotGateway
from .src.group_essence_extractor.db import EssenceRepository
from .src.group_essence_extractor.image_reply import ImageReplyCache
from .src.group_essence_extractor.plugin_config import PluginSettings
from .src.group_essence_extractor.plugin_identity import (
    PLUGIN_DATABASE_FILENAME,
    resolve_plugin_data_dir,
)
from .src.group_essence_extractor.plugin_service import (
    GroupEssencePluginService,
    PluginServiceError,
    format_search_page,
    format_sender_time_repair_report,
    format_status_report,
    format_sync_report,
    format_validation_report,
)
from .src.group_essence_extractor.runtime import (
    GroupEssenceRuntime,
    RuntimeConfig,
)

class GroupEssencePlugin(Star):
    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | None = None,
    ) -> None:
        super().__init__(context)
        self.settings = PluginSettings.from_mapping(config)
        data_dir = resolve_plugin_data_dir(get_astrbot_data_path())
        self.image_cache = ImageReplyCache(data_dir / "reply_images")
        self.image_reply_lock = asyncio.Lock()
        repository = EssenceRepository(
            data_dir / PLUGIN_DATABASE_FILENAME,
            backup_dir=data_dir / "backups",
        )
        self.service = GroupEssencePluginService(
            source=AstrBotEssenceSource(),
            repository=repository,
            validation_detail_request_limit=(
                self.settings.max_validation_detail_requests
            ),
            sync_detail_request_limit=self.settings.max_sync_detail_requests,
            history_query_limit=self.settings.history_query_limit,
            detail_retry_base_minutes=self.settings.detail_retry_base_minutes,
            detail_retry_max_hours=self.settings.detail_retry_max_hours,
            timing_logger=logger.info,
        )
        self.gateway = AstrBotOneBotGateway(
            context,
            self.settings.onebot_platform_id,
        )
        self.runtime = GroupEssenceRuntime(
            service=self.service,
            repository=repository,
            action_context=self.gateway,
            config=RuntimeConfig.from_settings(self.settings),
            logger=logger,
            health_path=data_dir / "ge_health.json",
        )

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        block_reason = self.settings.scheduled_sync_block_reason
        if self.settings.enable_scheduled_sync and block_reason:
            logger.warning(
                "GroupEssence 计划同步未启动："
                f"category={block_reason}"
            )
        await self.runtime.start()

    @filter.command("精华验收")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def validate_essence(
        self,
        event: AstrMessageEvent,
        group_id: str = "",
    ):
        """只读检查当前 OneBot 精华消息契约，不写数据库。"""
        event.stop_event()
        target = self.settings.resolve_authorized_group(event, group_id)
        if target is None:
            yield event.plain_result("无权限或目标群未在允许列表中。")
            return

        try:
            report = await self.service.validate(event, target)
        except OneBotActionError as exc:
            logger.warning(f"GroupEssence 验收失败：{exc.public_message}")
            yield event.plain_result(f"精华验收失败：{exc.public_message}")
            return
        except PluginServiceError as exc:
            logger.warning(f"GroupEssence 验收失败：category={exc.category}")
            yield event.plain_result(f"精华验收失败：{exc.public_message}")
            return
        except Exception as exc:
            logger.error(f"GroupEssence 验收失败：category={type(exc).__name__}")
            yield event.plain_result("精华验收失败：内部错误。")
            return

        logger.info(f"GroupEssence 验收完成：collected={report.collected}")
        yield event.plain_result(format_validation_report(report))

    @filter.command("精华状态")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def essence_status(self, event: AstrMessageEvent):
        """显示脱敏后的插件模式和数据库状态。"""
        event.stop_event()
        if not self.settings.is_admin(event):
            yield event.plain_result("无权限。")
            return

        try:
            report = await self.service.status()
        except PluginServiceError as exc:
            logger.warning(f"GroupEssence 状态读取失败：category={exc.category}")
            yield event.plain_result(f"精华状态读取失败：{exc.public_message}")
            return
        except Exception as exc:
            logger.error(f"GroupEssence 状态读取失败：category={type(exc).__name__}")
            yield event.plain_result("精华状态读取失败：内部错误。")
            return

        yield event.plain_result(
            format_status_report(
                report,
                validation_mode=self.settings.validation_mode,
                allowed_group_count=len(self.settings.allowed_group_ids),
                runtime=self.runtime.snapshot(),
            )
        )

    @filter.command("精华同步")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def sync_essence(
        self,
        event: AstrMessageEvent,
        group_id: str = "",
    ):
        """同步已授权群的精华消息；只读验收模式下拒绝执行。"""
        event.stop_event()
        target = self.settings.resolve_authorized_group(event, group_id)
        if target is None:
            yield event.plain_result("无权限或目标群未在允许列表中。")
            return
        if self.settings.validation_mode:
            yield event.plain_result("当前处于只读验收模式，未执行同步。")
            return

        try:
            report = await self.service.sync(event, target)
        except OneBotActionError as exc:
            logger.warning(f"GroupEssence 同步失败：{exc.public_message}")
            yield event.plain_result(f"精华同步失败：{exc.public_message}")
            return
        except PluginServiceError as exc:
            logger.warning(f"GroupEssence 同步失败：category={exc.category}")
            yield event.plain_result(f"精华同步失败：{exc.public_message}")
            return
        except Exception as exc:
            logger.error(f"GroupEssence 同步失败：category={type(exc).__name__}")
            yield event.plain_result("精华同步失败：内部错误。")
            return

        logger.info(
            "GroupEssence 同步完成："
            f"collected={report.collected}, inserted={report.inserted}, "
            f"updated={report.updated}, refreshed={report.refreshed}, "
            f"unchanged={report.unchanged}, "
            f"sender_times_enriched={report.sender_times_enriched}, "
            f"history_lookup_failed={report.history_lookup_failed}, "
            f"detail_failures={report.detail_failures}, "
            f"detail_deferred={report.detail_deferred}"
        )
        yield event.plain_result(format_sync_report(report))

    @filter.command("精华补全时间")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def repair_sender_times(
        self,
        event: AstrMessageEvent,
        count: str = "",
    ):
        """以有界群历史补全数据库中缺失的真实发送时间。"""
        event.stop_event()
        target = self.settings.resolve_authorized_group(event)
        if target is None:
            yield event.plain_result("无权限或目标群未在允许列表中。")
            return
        if self.settings.validation_mode:
            yield event.plain_result("当前处于只读验收模式，未执行补全。")
            return

        try:
            report = await self.service.repair_sender_times(event, target, count)
        except OneBotActionError as exc:
            logger.warning(f"GroupEssence 发送时间补全失败：{exc.public_message}")
            yield event.plain_result(f"发送时间补全失败：{exc.public_message}")
            return
        except PluginServiceError as exc:
            logger.warning(f"GroupEssence 发送时间补全失败：category={exc.category}")
            yield event.plain_result(f"发送时间补全失败：{exc.public_message}")
            return
        except Exception as exc:
            logger.error(
                f"GroupEssence 发送时间补全失败：category={type(exc).__name__}"
            )
            yield event.plain_result("发送时间补全失败：内部错误。")
            return

        logger.info(
            "GroupEssence 发送时间补全完成："
            f"history_scanned={report.history_scanned}, "
            f"candidates={report.candidates}, matched={report.matched}, "
            f"updated={report.updated}, remaining={report.remaining}"
        )
        yield event.plain_result(format_sender_time_repair_report(report))

    @filter.command("精华查询")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def search_essence(
        self,
        event: AstrMessageEvent,
        keyword: str = "",
    ):
        """在当前已授权群的本地精华记录中查询正文。"""
        event.stop_event()
        target = self.settings.resolve_authorized_group(event)
        if target is None:
            yield event.plain_result("无权限或目标群未在允许列表中。")
            return
        if self.settings.validation_mode:
            yield event.plain_result("当前处于只读验收模式，未执行查询。")
            return
        keyword = str(keyword or "").strip()
        if not keyword:
            yield event.plain_result("查询关键词不能为空。")
            return

        try:
            page = await self.service.search(
                target,
                keyword,
                self.settings.max_query_results,
            )
        except PluginServiceError as exc:
            logger.warning(f"GroupEssence 查询失败：category={exc.category}")
            yield event.plain_result(f"精华查询失败：{exc.public_message}")
            return
        except Exception as exc:
            logger.error(f"GroupEssence 查询失败：category={type(exc).__name__}")
            yield event.plain_result("精华查询失败：内部错误。")
            return

        logger.info(f"GroupEssence 查询完成：count={len(page.items)}, total={page.total}")
        async for result in self._query_results(event, page, "精华查询结果"):
            yield result

    @filter.command("精华最近")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def recent_essence(
        self,
        event: AstrMessageEvent,
        count: str = "",
    ):
        """显示当前已授权群最近设置的精华消息。"""
        event.stop_event()
        target = self.settings.resolve_authorized_group(event)
        if target is None:
            yield event.plain_result("无权限或目标群未在允许列表中。")
            return
        if self.settings.validation_mode:
            yield event.plain_result("当前处于只读验收模式，未执行查询。")
            return
        try:
            limit = int(count) if str(count or "").strip() else self.settings.max_query_results
        except ValueError:
            yield event.plain_result("数量必须是 1 到 20 之间的整数。")
            return
        if not 1 <= limit <= 20:
            yield event.plain_result("数量必须是 1 到 20 之间的整数。")
            return

        try:
            page = await self.service.recent(target, limit)
        except PluginServiceError as exc:
            logger.warning(f"GroupEssence 最近记录读取失败：category={exc.category}")
            yield event.plain_result(f"精华最近读取失败：{exc.public_message}")
            return
        except Exception as exc:
            logger.error(f"GroupEssence 最近记录读取失败：category={type(exc).__name__}")
            yield event.plain_result("精华最近读取失败：内部错误。")
            return

        logger.info(f"GroupEssence 最近记录读取完成：count={len(page.items)}")
        async for result in self._query_results(event, page, "最近精华"):
            yield result

    async def _query_results(self, event, page, title):
        # Send text first: unavailable/expired images never erase the query results.
        yield event.plain_result(
            format_search_page(
                page,
                max_content_chars=self.settings.max_content_chars,
                title=title,
            )
        )
        if not self.settings.max_reply_images or not page.items:
            return
        async with self.image_reply_lock:
            images, omitted = await asyncio.to_thread(
                self.image_cache.prepare, page.items, self.settings.max_reply_images,
            )
        failures = 0
        for image in images:
            label = f"精华 [{image.record_number}/{len(page.items)}] 图片 {image.image_number}/{image.image_total}"
            if image.error:
                failures += 1
                yield event.plain_result(f"{label}：{image.error}")
            else:
                # Bytes, not a file path: AstrBot and NapCat have separate filesystems.
                yield event.chain_result([Plain(label + "\n"), Image.fromBytes(image.data)])
        if omitted:
            yield event.plain_result(f"本次达到图片数量上限，另有 {omitted} 张未发送。可缩小查询范围。")
        if images:
            logger.info(f"GroupEssence 图片准备完成：count={len(images)}, failed={failures}, omitted={omitted}")

    async def terminate(self) -> None:
        await self.runtime.stop()
