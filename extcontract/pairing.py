"""Registration desk (报名台) for the joint-exhibition pairing service.

Two branch libraries each file their own consent forms (认同单); neither
branch ever holds the whole pairing roster.  The desk receives both streams
through the *original* ingestion entry point — one ``IngestionLibrary`` per
side, each fixed to its own reader contract (its frozen version) — and only
the desk can join the two halves.

A registration (报名号, the shared ``row_id``) is approved only when each
direction's *current* form independently stands:

  * the row exists at the frozen reader version;
  * its consent column carries an explicit ``同意`` that arrived through a
    real writer column — a ``同意`` the reader itself filled in for a missing
    column (``fill_default``) is the contract's own default, not a personal
    signature, so the form stays pending (待签);
  * an explicit ``撤回`` never stands.

The two directions merge only for the *same* registration with the same
round (轮次) and the same exhibition group (展组); the desk never splices
the left end of one registration onto the right end of another.  A pair may
be supported by several different valid registrations and leaves the
signed-out relation only when its last supporting registration disappears.

Member numbers are interpreted through the stable ``field_id`` pairing and
the real reader domain: the desk keys members by the *canonical* reader
values held by the ingestion libraries, never by transport spellings or
display names.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contract import Contract
from .errors import ContractError
from .ingest import IngestionLibrary, IngestResult, RowState

# Consent vocabulary (the TEXT domain of the consent column).
CONSENT_AGREE = "同意"
CONSENT_WITHDRAW = "撤回"

# Stable field ids of the consent form, shared by both directions.
FIELD_MEMBER = "member_no"
FIELD_GROUP = "group_no"
FIELD_ROUND = "round"
FIELD_CONSENT = "consent"

_SIDE_LEFT = "left"
_SIDE_RIGHT = "right"
_SIDES = (_SIDE_LEFT, _SIDE_RIGHT)


def _sort_key(value: Any) -> str:
    """Deterministic order for canonical values of possibly mixed types."""
    return f"{type(value).__name__}:{value!r}"


@dataclass
class SideView:
    """One direction's current form for a registration, at the frozen version."""

    side: str
    present: bool
    status: str  # "standing" | "missing" | "pending_default" | "withdrawn" | "not_signed"
    revision: int | None = None
    member: Any = None
    group: Any = None
    round: Any = None
    consent: Any = None
    consent_source: str | None = None  # "writer" | "default" | None

    def to_json(self) -> dict:
        return {
            "side": self.side,
            "present": self.present,
            "status": self.status,
            "revision": self.revision,
            "member": self.member,
            "group": self.group,
            "round": self.round,
            "consent": self.consent,
            "consent_source": self.consent_source,
        }


@dataclass
class RegistrationView:
    """The desk's merged view of one 报名号 (never a splice of two rows)."""

    registration: str
    left: SideView
    right: SideView
    approved: bool
    pair: tuple | None  # (group, left_member, right_member) when approved
    reasons: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "registration": self.registration,
            "approved": self.approved,
            "pair": list(self.pair) if self.pair is not None else None,
            "reasons": list(self.reasons),
            "left": self.left.to_json(),
            "right": self.right.to_json(),
        }


@dataclass
class PairingReport:
    """Frozen-version desk report: why pairs stand, what is missing/pending."""

    left_version: str
    right_version: str
    approved_pairs: list[dict]
    registrations: list[RegistrationView]
    one_sided: list[str]  # 缺另一方: registrations present on only one side
    pending_default: list[str]  # 待签: consent came from a reader default
    withdrawn: list[str]  # explicit 撤回 on at least one side
    mismatched: list[str]  # both sides stand but group/round differ
    not_signed: list[str]  # any other non-consent value

    def to_json(self) -> dict:
        return {
            "left_version": self.left_version,
            "right_version": self.right_version,
            "approved_pairs": _json_copy(self.approved_pairs),
            "registrations": [v.to_json() for v in self.registrations],
            "one_sided": list(self.one_sided),
            "pending_default": list(self.pending_default),
            "withdrawn": list(self.withdrawn),
            "mismatched": list(self.mismatched),
            "not_signed": list(self.not_signed),
        }


def _json_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _json_copy(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_copy(v) for v in value]
    return value


class PairingDesk:
    """Two fixed-reader ingestion libraries plus the merge that neither
    branch can perform on its own."""

    def __init__(self, left_reader: Contract, right_reader: Contract):
        for name, contract in (("left_reader", left_reader), ("right_reader", right_reader)):
            if contract.role != "reader":
                raise ContractError(f"{name} must be parsed with role='reader'")
            ids = {f.field_id for f in contract.fields}
            required = {FIELD_MEMBER, FIELD_GROUP, FIELD_ROUND, FIELD_CONSENT}
            if not required <= ids:
                raise ContractError(
                    f"{name} must declare fields {sorted(required)}, got {sorted(ids)}"
                )
        self.left_library = IngestionLibrary(left_reader)
        self.right_library = IngestionLibrary(right_reader)

    # -- original ingestion entry points ------------------------------------

    @property
    def left_version(self) -> str:
        return self.left_library.reader_version

    @property
    def right_version(self) -> str:
        return self.right_library.reader_version

    def ingest_left(self, writer: Contract, document: Any) -> IngestResult:
        """Ingest one left-branch consent file (atomic, revision-aware)."""
        return self.left_library.ingest(writer, document)

    def ingest_right(self, writer: Contract, document: Any) -> IngestResult:
        """Ingest one right-branch consent file (atomic, revision-aware)."""
        return self.right_library.ingest(writer, document)

    # -- merge ---------------------------------------------------------------

    def _side_view(self, side: str, row: RowState | None) -> SideView:
        if row is None:
            return SideView(side=side, present=False, status="missing")
        canonical = row.canonical
        consent = canonical.get(FIELD_CONSENT)
        source = row.sources.get(FIELD_CONSENT) or {}
        consent_source = (
            "default" if source.get("conversion") == "fill_default" else "writer"
        )
        if consent == CONSENT_AGREE and consent_source == "writer":
            status = "standing"
        elif consent == CONSENT_AGREE:
            # The 同意字样 was filled in for a missing writer column; it is the
            # contract's default, not the person's signature.
            status = "pending_default"
        elif consent == CONSENT_WITHDRAW:
            status = "withdrawn"
        else:
            status = "not_signed"
        return SideView(
            side=side,
            present=True,
            status=status,
            revision=row.revision,
            member=canonical.get(FIELD_MEMBER),
            group=canonical.get(FIELD_GROUP),
            round=canonical.get(FIELD_ROUND),
            consent=consent,
            consent_source=consent_source,
        )

    def registration_views(self) -> list[RegistrationView]:
        """Current merged view of every registration known on either side."""
        left_rows = {row.row_id: row for row in self.left_library.snapshot()}
        right_rows = {row.row_id: row for row in self.right_library.snapshot()}
        views: list[RegistrationView] = []
        for registration in sorted(set(left_rows) | set(right_rows)):
            left = self._side_view(_SIDE_LEFT, left_rows.get(registration))
            right = self._side_view(_SIDE_RIGHT, right_rows.get(registration))
            reasons: list[str] = []
            for view in (left, right):
                if view.status == "missing":
                    reasons.append(f"{view.side}_missing")
                elif view.status == "pending_default":
                    reasons.append(f"{view.side}_pending_default")
                elif view.status == "withdrawn":
                    reasons.append(f"{view.side}_withdrawn")
                elif view.status == "not_signed":
                    reasons.append(f"{view.side}_not_signed")
            approved = False
            pair = None
            if left.status == "standing" and right.status == "standing":
                if left.group != right.group:
                    reasons.append("group_mismatch")
                if left.round != right.round:
                    reasons.append("round_mismatch")
                if not reasons:
                    approved = True
                    pair = (left.group, left.member, right.member)
            views.append(
                RegistrationView(
                    registration=registration,
                    left=left,
                    right=right,
                    approved=approved,
                    pair=pair,
                    reasons=reasons,
                )
            )
        return views

    def report(self) -> PairingReport:
        """Frozen-version report: why each pair stands; what blocks the rest."""
        views = self.registration_views()
        supporters: dict[tuple, list[dict]] = {}
        for view in views:
            if view.approved:
                supporters.setdefault(view.pair, []).append(
                    {
                        "registration": view.registration,
                        "left_revision": view.left.revision,
                        "right_revision": view.right.revision,
                    }
                )
        approved_pairs = [
            {
                "group": group,
                "left_member": left_member,
                "right_member": right_member,
                "registrations": sorted(sups, key=lambda s: s["registration"]),
                "support_count": len(sups),
            }
            for (group, left_member, right_member), sups in sorted(
                supporters.items(), key=lambda item: _sort_key(item[0])
            )
        ]

        def listed(*statuses: str) -> list[str]:
            return [
                v.registration
                for v in views
                if any(getattr(v, side).status in statuses for side in _SIDES)
            ]

        return PairingReport(
            left_version=self.left_version,
            right_version=self.right_version,
            approved_pairs=approved_pairs,
            registrations=views,
            one_sided=[
                v.registration
                for v in views
                if (v.left.status == "missing") != (v.right.status == "missing")
            ],
            pending_default=listed("pending_default"),
            withdrawn=listed("withdrawn"),
            mismatched=[
                v.registration
                for v in views
                if "group_mismatch" in v.reasons or "round_mismatch" in v.reasons
            ],
            not_signed=listed("not_signed"),
        )

    def sign_out(self) -> dict[Any, list[tuple]]:
        """签出: the real binary relation, deduplicated per exhibition group.

        Returns ``{group: [(left_member, right_member), ...]}`` sorted
        deterministically; directly consumable by the distribution desk.
        """
        relation: dict[Any, set[tuple]] = {}
        for view in self.registration_views():
            if view.approved:
                group, left_member, right_member = view.pair
                relation.setdefault(group, set()).add((left_member, right_member))
        return {
            group: sorted(pairs, key=_sort_key)
            for group, pairs in sorted(relation.items(), key=lambda kv: _sort_key(kv[0]))
        }
