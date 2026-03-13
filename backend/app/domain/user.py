"""Доменная сущность пользователя."""

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from .exceptions import InvalidEmailError

_EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")


@dataclass
class User:
    email: str
    name: str = ""
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self) -> None:
        if not _EMAIL_REGEX.match(self.email):
            raise InvalidEmailError(self.email)
