from tinkerfin_studio.attachments.entity import (
    AttachmentCollection,
    AttachmentFile,
    AttachmentReference,
)
from tinkerfin_studio.auth.models import User
from tinkerfin_studio.conversation.models import (
    ConversationInterruptClaim,
    ConversationRunRegistration,
    ConversationThread,
)
from tinkerfin_studio.infrastructure.database import Base
from tinkerfin_studio.models.entity import AgentModel, ModelConnection


def test_business_schema_contains_no_foreign_keys() -> None:
    """Studio 表间引用完整性必须由应用层维护"""

    registered = (
        User,
        AttachmentFile,
        AttachmentCollection,
        AttachmentReference,
        AgentModel,
        ModelConnection,
        ConversationThread,
        ConversationRunRegistration,
        ConversationInterruptClaim,
    )
    assert {model.__tablename__ for model in registered} == set(Base.metadata.tables)
    assert Base.metadata.tables
    assert all(not table.foreign_keys for table in Base.metadata.tables.values())
