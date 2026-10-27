from dataclasses import dataclass, field
from typing import Any, ClassVar


@dataclass
class BalanceResult:
    value: float
    currency: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderField:
    key: str
    label: str
    kind: str = "text"  # text | secret | number | url
    required: bool = True
    placeholder: str = ""
    help: str = ""

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__


class BalanceProvider:
    """Base class for a balance source. Subclasses set `key`, `title`, `fields`, and
    implement `fetch(config)`. Keep implementations small and stateless."""

    key: ClassVar[str] = ""
    title: ClassVar[str] = ""
    description: ClassVar[str] = ""
    fields: ClassVar[list[ProviderField]] = []

    async def fetch(self, config: dict[str, Any]) -> BalanceResult:  # pragma: no cover
        raise NotImplementedError

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> None:
        missing = [f.label for f in cls.fields if f.required and not config.get(f.key)]
        if missing:
            raise ValueError("Не заполнены поля: " + ", ".join(missing))
