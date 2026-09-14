"""附件归属、发布、读取与清理业务"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

import anyio
from anyio import to_thread
from pydantic import JsonValue
from sqlalchemy import delete, exists, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin.media import AttachmentContent
from tinkerfin_contracts.media import Attachment
from tinkerfin_studio.api.errors import AttachmentErrorCode, BusinessException
from tinkerfin_studio.attachments.documents import DocumentProcessor
from tinkerfin_studio.attachments.entity import (
    AttachmentCollection,
    AttachmentFile,
    AttachmentReference,
)
from tinkerfin_studio.attachments.processing import (
    MAX_FILE_BYTES,
    MAX_TOTAL_BYTES,
    MIME_TYPES,
    image_variant,
    validate_file,
)
from tinkerfin_studio.attachments.storage import (
    AttachmentStorage,
    DownloadLink,
    UploadForm,
)
from tinkerfin_studio.conversation.models import ConversationThread
from tinkerfin_studio.infrastructure.database import Database

logger = logging.getLogger(__name__)


async def byte_chunks(data: bytes) -> AsyncIterator[bytes]:
    """把已限制大小的工具结果交给同一存储入口"""
    yield data


def descriptor(row: AttachmentFile) -> Attachment:
    """返回可以公开的附件描述，不暴露存储细节"""
    return Attachment(
        id=row.id, name=row.name, mime_type=row.mime_type, size_bytes=row.size_bytes
    )


class AttachmentService:
    """管理附件对象与业务数据库，数据库事务不跨文件或解析 I/O"""

    def __init__(self, database: Database, storage: AttachmentStorage) -> None:
        self.documents = DocumentProcessor()
        self._database = database
        self._storage = storage
        self._processing = anyio.CapacityLimiter(2)
        self._uploads = anyio.CapacityLimiter(2)

    async def upload(
        self,
        *,
        user_id: int,
        name: str,
        chunks: AsyncIterator[bytes],
        thread_id: str | None = None,
        collection_id: str | None = None,
        source: Literal["user", "tool"] = "user",
    ) -> Attachment:
        """有界接收并验证文件，完整保存原件和派生图后发布

        上传中断和进程崩溃留下的 staging 记录由清理入口回收。
        当前进程取消时完成对象清理，再传播取消，不发布半成品。
        """
        if thread_id is not None and collection_id is not None:
            raise ValueError("附件只能属于会话或集合中的一种范围")
        async with self._uploads:
            self._validate_name(name)
            content = bytearray()
            try:
                with anyio.fail_after(60):
                    async for chunk in chunks:
                        if len(content) + len(chunk) > MAX_FILE_BYTES:
                            raise BusinessException(AttachmentErrorCode.TOO_LARGE)
                        content.extend(chunk)
            except TimeoutError as error:
                raise BusinessException(AttachmentErrorCode.UPLOAD_TIMEOUT) from error
            data = bytes(content)
            try:
                mime = await to_thread.run_sync(
                    validate_file, name, data, limiter=self._processing
                )
            except (ValueError, OSError, Warning) as error:
                raise BusinessException(AttachmentErrorCode.INVALID_FILE) from error
            row = AttachmentFile(
                id=uuid4().hex,
                user_id=user_id,
                thread_id=thread_id,
                name=name,
                mime_type=mime,
                size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                status="processing",
                source=source,
            )
            async with self._database.session() as session:
                if thread_id is not None:
                    await self._require_thread(session, user_id, thread_id)
                if collection_id is not None:
                    await self._require_collection(
                        session, user_id, collection_id, writable=True
                    )
                session.add(row)
                await session.commit()
            try:
                return await self._publish(row, data, mime, collection_id=collection_id)
            except BaseException:
                await self._discard(row.id)
                raise

    @staticmethod
    def _validate_name(name: str) -> None:
        if (
            not name
            or len(name) > 255
            or any(c in name for c in ("/", "\\", "\0", "\r", "\n"))
            or name.rsplit(".", 1)[-1].lower() not in MIME_TYPES
        ):
            raise BusinessException(AttachmentErrorCode.INVALID_FILE)

    async def request_upload(
        self, *, user_id: int, name: str, size_bytes: int
    ) -> tuple[str, UploadForm]:
        """登记用户待上传文件，只签发该附件的临时写入许可"""
        self._validate_name(name)
        if not 0 < size_bytes <= MAX_FILE_BYTES:
            raise BusinessException(AttachmentErrorCode.TOO_LARGE)
        row = AttachmentFile(
            id=uuid4().hex,
            user_id=user_id,
            name=name,
            size_bytes=size_bytes,
            mime_type="",
            status="uploading",
            source="user",
        )
        async with self._database.session() as session:
            session.add(row)
            await session.commit()
        try:
            return row.id, await self._storage.upload_form(row.id, size_bytes)
        except BaseException:
            await self._discard(row.id)
            raise

    async def complete_upload(self, attachment_id: str, *, user_id: int) -> Attachment:
        """校验直传文件并发布；已完成的重复请求返回同一附件

        条件更新保证同一附件只有一个处理者，外部 I/O 不持数据库锁。
        仅已读取并验证的字节进入正式对象，晚到的临时上传不能覆盖发布结果。
        """
        async with self._uploads:
            async with self._database.session() as session:
                row = await session.get(AttachmentFile, attachment_id)
                if row is None or row.user_id != user_id:
                    raise BusinessException(AttachmentErrorCode.NOT_FOUND)
                if row.status == "ready":
                    return await self.get(attachment_id, user_id=user_id)
                connection = await session.connection()
                claimed = await connection.execute(
                    update(AttachmentFile)
                    .where(
                        AttachmentFile.id == attachment_id,
                        AttachmentFile.user_id == user_id,
                        AttachmentFile.status == "uploading",
                    )
                    .values(status="processing")
                )
                if claimed.rowcount != 1:
                    raise BusinessException(AttachmentErrorCode.UPLOAD_IN_PROGRESS)
                await session.commit()
            try:
                data = await self._storage.read(attachment_id + "-upload")
                if len(data) != row.size_bytes:
                    raise BusinessException(AttachmentErrorCode.INVALID_FILE)
                try:
                    mime = await to_thread.run_sync(
                        validate_file, row.name, data, limiter=self._processing
                    )
                except (ValueError, OSError, Warning) as error:
                    raise BusinessException(AttachmentErrorCode.INVALID_FILE) from error
                return await self._publish(row, data, mime)
            except FileNotFoundError as error:
                await self._discard(attachment_id)
                raise BusinessException(AttachmentErrorCode.NOT_FOUND) from error
            except BaseException:
                await self._discard(attachment_id)
                raise

    async def _publish(
        self,
        row: AttachmentFile,
        data: bytes,
        mime: str,
        *,
        collection_id: str | None = None,
    ) -> Attachment:
        await self._storage.put(row.id, byte_chunks(data))
        if mime.startswith("image/"):
            for suffix, size in (("-preview", 480), ("-model", 2048)):
                variant = await to_thread.run_sync(
                    image_variant, data, size, limiter=self._processing
                )
                await self._storage.put(row.id + suffix, byte_chunks(variant))
        await self._storage.delete(row.id + "-upload")
        async with self._database.session() as session:
            saved = await session.scalar(
                select(AttachmentFile)
                .where(AttachmentFile.id == row.id)
                .with_for_update()
            )
            if saved is None or saved.status != "processing":
                raise BusinessException(AttachmentErrorCode.NOT_FOUND)
            if collection_id is not None:
                await self._require_collection(
                    session, row.user_id, collection_id, writable=True
                )
                session.add(
                    AttachmentReference(
                        collection_id=collection_id, attachment_id=row.id
                    )
                )
            saved.mime_type = mime
            saved.sha256 = hashlib.sha256(data).hexdigest()
            saved.status = "ready"
            await session.commit()
            return descriptor(saved)

    async def _discard(self, attachment_id: str) -> None:
        # 原错误或取消必须保留；删除失败的记录交给已有清理入口回收
        with anyio.CancelScope(shield=True):
            try:
                with anyio.fail_after(30):
                    async with self._database.session() as session:
                        row = await session.get(AttachmentFile, attachment_id)
                        if row is None or row.status == "ready":
                            return
                        row.status = "deleting"
                        await session.commit()
                    await self._delete(attachment_id)
            except Exception:
                logger.exception("附件清理失败，等待后续回收：%s", attachment_id)

    async def download_url(
        self,
        attachment_id: str,
        *,
        user_id: int,
        variant: Literal["original", "preview"] = "original",
    ) -> DownloadLink:
        """鉴权后签发原件或图片预览的短期地址，不持久化读取许可"""
        attachment = await self.get(attachment_id, user_id=user_id)
        if variant == "preview" and not attachment.mime_type.startswith("image/"):
            raise BusinessException(AttachmentErrorCode.INVALID_VARIANT)
        return await self._storage.download_link(
            attachment_id + ("-preview" if variant == "preview" else ""),
            name=attachment.name,
            mime_type="image/jpeg" if variant == "preview" else attachment.mime_type,
            inline=variant == "preview",
        )

    async def _require_collection(
        self,
        session: AsyncSession,
        user_id: int,
        collection_id: str,
        *,
        writable: bool = False,
    ) -> AttachmentCollection:
        collection = await session.get(AttachmentCollection, collection_id)
        if (
            collection is None
            or collection.user_id != user_id
            or (writable and collection.purpose != "execution")
        ):
            raise BusinessException(AttachmentErrorCode.NOT_FOUND)
        return collection

    async def create_collection(
        self,
        *,
        user_id: int,
        collection_id: str,
        purpose: Literal["input", "execution"],
        attachment_ids: tuple[str, ...],
        configuration: dict[str, JsonValue],
        task_id: str | None = None,
    ) -> None:
        """在提交调度前保留不可变输入；重复请求只能复用完全相同的配置和文件"""
        if len(attachment_ids) > 5 or len(set(attachment_ids)) != len(attachment_ids):
            raise BusinessException(AttachmentErrorCode.INVALID_FILE)
        try:
            async with self._database.session() as session:
                existing = await session.get(AttachmentCollection, collection_id)
                if existing is not None:
                    await self._verify_collection(
                        session,
                        existing,
                        user_id=user_id,
                        collection_id=collection_id,
                        purpose=purpose,
                        attachment_ids=attachment_ids,
                        configuration=configuration,
                    )
                    return
                files: list[AttachmentFile] = []
                for identity in sorted(attachment_ids):
                    row = await session.scalar(
                        select(AttachmentFile)
                        .where(AttachmentFile.id == identity)
                        .with_for_update()
                    )
                    if row is None or row.user_id != user_id or row.status != "ready":
                        raise BusinessException(AttachmentErrorCode.NOT_FOUND)
                    files.append(row)
                if sum(row.size_bytes for row in files) > MAX_TOTAL_BYTES:
                    raise BusinessException(AttachmentErrorCode.TOO_LARGE)
                session.add(
                    AttachmentCollection(
                        id=collection_id,
                        user_id=user_id,
                        purpose=purpose,
                        configuration=configuration,
                        task_id=task_id,
                    )
                )
                for row in files:
                    session.add(
                        AttachmentReference(
                            collection_id=collection_id, attachment_id=row.id
                        )
                    )
                await session.commit()
        except IntegrityError:
            async with self._database.session() as session:
                existing = await session.get(AttachmentCollection, collection_id)
                if existing is None:
                    raise
                await self._verify_collection(
                    session,
                    existing,
                    user_id=user_id,
                    collection_id=collection_id,
                    purpose=purpose,
                    attachment_ids=attachment_ids,
                    configuration=configuration,
                )

    async def _verify_collection(
        self,
        session: AsyncSession,
        existing: AttachmentCollection,
        *,
        user_id: int,
        collection_id: str,
        purpose: str,
        attachment_ids: tuple[str, ...],
        configuration: dict[str, JsonValue],
    ) -> None:
        if (
            existing.user_id != user_id
            or existing.purpose != purpose
            or existing.configuration != configuration
        ):
            raise BusinessException(AttachmentErrorCode.REFERENCE_CONFLICT)
        if purpose == "input":
            saved_ids = set(
                await session.scalars(
                    select(AttachmentReference.attachment_id).where(
                        AttachmentReference.collection_id == collection_id
                    )
                )
            )
            if saved_ids != set(attachment_ids):
                raise BusinessException(AttachmentErrorCode.REFERENCE_CONFLICT)

    async def list_collection(
        self, *, user_id: int, collection_id: str
    ) -> list[Attachment]:
        """读取当前任务输入或运行结果中的附件，不扩大到用户其他文件"""
        async with self._database.session() as session:
            await self._require_collection(session, user_id, collection_id)
            rows = await session.scalars(
                select(AttachmentFile)
                .join(
                    AttachmentReference,
                    AttachmentReference.attachment_id == AttachmentFile.id,
                )
                .where(
                    AttachmentReference.collection_id == collection_id,
                    AttachmentFile.user_id == user_id,
                    AttachmentFile.status == "ready",
                )
                .order_by(AttachmentFile.created_at, AttachmentFile.id)
                .limit(100)
            )
            return [descriptor(row) for row in rows]

    async def collection_created_at(
        self, *, user_id: int, collection_id: str
    ) -> datetime:
        """读取输入集合的固定创建时间，供间隔日程在重复请求时保持同一起点"""
        async with self._database.session() as session:
            collection = await self._require_collection(session, user_id, collection_id)
            return collection.created_at.replace(tzinfo=UTC)

    async def mark_collection_task(
        self, *, user_id: int, collection_id: str, task_id: str
    ) -> None:
        """登记任务引用，保留历史配置而不改写其内容"""
        async with self._database.session() as session:
            collection = await self._require_collection(session, user_id, collection_id)
            if collection.task_id is not None and collection.task_id != task_id:
                raise BusinessException(AttachmentErrorCode.REFERENCE_CONFLICT)
            collection.task_id = task_id
            await session.commit()

    async def discard_collection(self, *, user_id: int, collection_id: str) -> None:
        """仅回收确认未提交任务的输入集合；不删除文件或运行历史"""
        async with self._database.session() as session:
            collection = await self._require_collection(session, user_id, collection_id)
            if collection.task_id is not None or collection.purpose != "input":
                raise BusinessException(AttachmentErrorCode.ALREADY_SENT)
            await session.execute(
                delete(AttachmentReference).where(
                    AttachmentReference.collection_id == collection_id
                )
            )
            await session.delete(collection)
            await session.commit()

    async def _require_thread(
        self, session: AsyncSession, user_id: int, thread_id: str
    ) -> None:
        thread = await session.scalar(
            select(ConversationThread).where(
                ConversationThread.thread_id == thread_id,
                ConversationThread.user_id == user_id,
            )
        )
        if thread is None or thread.status == "deleting":
            raise BusinessException(AttachmentErrorCode.THREAD_UNAVAILABLE)

    async def get(
        self,
        attachment_id: str,
        *,
        user_id: int,
        thread_id: str | None = None,
        collection_id: str | None = None,
    ) -> Attachment:
        """检查用户与会话归属，失效或未发布附件不提供内容"""
        async with self._database.session() as session:
            row = await session.get(AttachmentFile, attachment_id)
            if row is None or row.user_id != user_id or row.status != "ready":
                raise BusinessException(AttachmentErrorCode.NOT_FOUND)
            if thread_id is not None and row.thread_id not in (None, thread_id):
                raise BusinessException(AttachmentErrorCode.NOT_FOUND)
            if collection_id is not None:
                await self._require_collection(session, user_id, collection_id)
                reference = await session.get(
                    AttachmentReference, (collection_id, attachment_id)
                )
                if reference is None:
                    raise BusinessException(AttachmentErrorCode.NOT_FOUND)
            elif row.thread_id is not None:
                retained = await session.scalar(
                    select(
                        exists().where(
                            AttachmentReference.attachment_id == attachment_id
                        )
                    )
                )
                if not retained:
                    await self._require_thread(session, user_id, row.thread_id)
            return descriptor(row)

    async def read(
        self,
        attachment_id: str,
        *,
        user_id: int,
        thread_id: str | None = None,
        collection_id: str | None = None,
        variant: Literal["original", "preview", "model"] = "original",
    ) -> tuple[Attachment, bytes]:
        """鉴权后读取原件或预生成的图片派生文件"""
        attachment = await self.get(
            attachment_id,
            user_id=user_id,
            thread_id=thread_id,
            collection_id=collection_id,
        )
        if variant not in {"original", "preview", "model"}:
            raise BusinessException(AttachmentErrorCode.INVALID_VARIANT)
        suffix = "" if variant == "original" else f"-{variant}"
        if suffix and not attachment.mime_type.startswith("image/"):
            raise BusinessException(AttachmentErrorCode.INVALID_VARIANT)
        try:
            data = await self._storage.read(attachment.id + suffix)
        except FileNotFoundError as error:
            raise BusinessException(AttachmentErrorCode.NOT_FOUND) from error
        return attachment, data

    async def read_image(
        self,
        attachment: Attachment,
        *,
        user_id: int,
        thread_id: str | None = None,
        collection_id: str | None = None,
    ) -> AttachmentContent:
        """提供模型用图，原件失效时让框架明确标记为不可阅读"""
        try:
            _, data = await self.read(
                attachment.id,
                user_id=user_id,
                thread_id=thread_id,
                collection_id=collection_id,
                variant="model",
            )
        except BusinessException as error:
            if error.error_code.http_status == 404:
                raise FileNotFoundError("附件图片不可用") from error
            raise
        return AttachmentContent(data=data, mime_type="image/jpeg")

    async def list_thread(self, *, user_id: int, thread_id: str) -> list[Attachment]:
        """为长对话和压缩后的重新阅读列出当前会话附件"""
        async with self._database.session() as session:
            await self._require_thread(session, user_id, thread_id)
            rows = await session.scalars(
                select(AttachmentFile)
                .where(
                    AttachmentFile.user_id == user_id,
                    AttachmentFile.thread_id == thread_id,
                    AttachmentFile.status == "ready",
                )
                .order_by(AttachmentFile.created_at)
                .limit(100)
            )
            return [descriptor(row) for row in rows]

    async def bind(
        self,
        session: AsyncSession,
        ids: list[str],
        *,
        user_id: int,
        thread_id: str,
        message_id: str,
    ) -> None:
        """在调用方的运行登记事务内绑定附件，禁止跨会话认领"""
        total = 0
        for attachment_id in sorted(set(ids)):
            row = await session.scalar(
                select(AttachmentFile)
                .where(AttachmentFile.id == attachment_id)
                .with_for_update()
            )
            if (
                row is None
                or row.user_id != user_id
                or row.status != "ready"
                or row.thread_id not in (None, thread_id)
            ):
                raise BusinessException(AttachmentErrorCode.NOT_FOUND)
            total += row.size_bytes
            row.thread_id = thread_id
            if row.message_id is None:
                row.message_id = message_id
        if total > MAX_TOTAL_BYTES:
            raise BusinessException(AttachmentErrorCode.TOO_LARGE)
        await session.flush()

    async def remove_draft(self, attachment_id: str, *, user_id: int) -> None:
        """只允许移除本人尚未发送的附件，与发送认领互斥"""
        async with self._database.session() as session:
            row = await session.scalar(
                select(AttachmentFile)
                .where(AttachmentFile.id == attachment_id)
                .with_for_update()
            )
            if row is None or row.user_id != user_id:
                raise BusinessException(AttachmentErrorCode.NOT_FOUND)
            retained = await session.scalar(
                select(
                    exists().where(AttachmentReference.attachment_id == attachment_id)
                )
            )
            if row.thread_id is not None or retained:
                raise BusinessException(AttachmentErrorCode.ALREADY_SENT)
            if row.status == "processing":
                raise BusinessException(AttachmentErrorCode.UPLOAD_IN_PROGRESS)
            row.status = "deleting"
            await session.commit()
        await self._delete(attachment_id)

    async def _delete(self, attachment_id: str) -> None:
        for suffix in ("", "-preview", "-model", "-upload"):
            await self._storage.delete(attachment_id + suffix)
        async with self._database.session() as session:
            row = await session.get(AttachmentFile, attachment_id)
            if row is not None:
                await session.delete(row)
                await session.commit()

    async def cleanup(self) -> None:
        """回收超过 24 小时的草稿和已删除会话的附件，每批最多 100 个"""
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=24)
        async with self._database.session() as session:
            live_threads = select(ConversationThread.thread_id)
            rows = await session.scalars(
                select(AttachmentFile)
                .where(
                    ~exists().where(
                        AttachmentReference.attachment_id == AttachmentFile.id
                    ),
                    or_(
                        AttachmentFile.status == "deleting",
                        (
                            AttachmentFile.thread_id.is_(None)
                            & (AttachmentFile.created_at < cutoff)
                        ),
                        (
                            AttachmentFile.thread_id.is_not(None)
                            & AttachmentFile.thread_id.not_in(live_threads)
                        ),
                    ),
                )
                .limit(100)
                .with_for_update(skip_locked=True)
            )
            ids = []
            for row in rows:
                row.status = "deleting"
                ids.append(row.id)
            await session.commit()
        for attachment_id in ids:
            await self._delete(attachment_id)
