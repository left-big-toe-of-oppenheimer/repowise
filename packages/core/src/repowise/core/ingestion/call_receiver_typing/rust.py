"""What a Rust call returns, for locals and chains typed from a call."""

from __future__ import annotations

import re

from ..models import CallReceiver, CallSite, Symbol
from ..return_types import declared_return_type
from ..type_names import strip_type_arguments
from ._shared import _TYPE_KINDS

# A borrow is the value it borrows for method lookup (auto-deref).
_BORROW = re.compile(r"&(?:'\w+\s+)?(?:mut\s+)?")
# What ``?``, ``.unwrap()`` and ``.expect(..)`` unwrap: the first type argument
# of a ``Result`` or ``Option``, an ``io::Result<T>`` alias included.
# ``x.unwrap()`` and ``x.expect(msg)``: the call and its argument count.
_UNWRAP_CALLS = {"unwrap": 0, "expect": 1}
_UNWRAPPED = re.compile(r"(?:\w+::)*(?:Result|Option)<\s*(?P<inner>[^<>,]+(?:<[^<>]*>)?)\s*(?:,.*)?>")


def _written_return(signature: str, unwrapped: bool) -> str | None:
    """The type name *signature* returns, borrow and type arguments aside."""
    raw = _BORROW.sub("", declared_return_type(signature) or "", count=1).strip()
    if unwrapped:
        inner = _UNWRAPPED.fullmatch(raw)
        if inner is None:
            return None
        raw = _BORROW.sub("", inner.group("inner"), count=1).strip()
    return strip_type_arguments(raw).strip()


class RustReturnTypes:
    """Rust declared return types, read as the repository type they name."""

    def _rust_returned_type_id(self, symbol: Symbol, path: str, unwrapped: bool) -> str | None:
        """``_returned_type_id`` for a Rust function or method *symbol* declared in *path*.

        ``Self`` is the type the method's ``impl`` names; any other name is
        looked up as a constructor call in the callee's file would be. An
        *unwrapped* call yields a ``Result``'s or ``Option``'s ``T``, and a
        bare ``Result``/``Option`` types nothing, nor does a trait. Ceiling: a
        path-qualified or generic-parameter return type types nothing.
        """
        if symbol.kind == "variable":
            return None if unwrapped else self._rust_variant_enum(symbol, path)
        written = _written_return(symbol.signature or "", unwrapped)
        if written == "Self":
            type_id: str | None = self._caller_class_id(symbol.id)
        elif written is None or not written.isidentifier() or written in ("Result", "Option"):
            return None
        else:
            found = self._class_seen_from(path, symbol.id, written, symbol.start_line)
            type_id = None if found is None else found.callee_id
        owner = self._symbols_by_id.get(type_id or "")
        # A trait is no concrete type: its `Self` and a returned `dyn` are any implementor.
        if owner is None or owner.kind not in _TYPE_KINDS or owner.kind == "trait":
            return None
        return type_id

    def _rust_variant_enum(self, variant: Symbol, path: str) -> str | None:
        """The enum a tuple variant (``Error::Io(e)``) constructs: the one whose body holds it."""
        parsed = self._parsed_files.get(path)
        owners = [
            s
            for s in (parsed.symbols if parsed else ())
            if s.kind == "enum" and s.start_line <= variant.start_line <= s.end_line
        ]
        return min(owners, key=lambda s: s.end_line - s.start_line).id if owners else None

    def _rust_unwrapped_link(
        self, file_path: str, call: CallSite, inner: CallReceiver, caller_id: str
    ) -> str | None:
        """The type a chain link ``f().unwrap()`` yields: the ``T`` of ``f``'s ``Result``/``Option``."""
        if _UNWRAP_CALLS.get(inner.target_name) != inner.argument_count or inner.receiver_name:
            return None
        nested = self._nested_receiver_call(file_path, call.line, inner, "rust")
        if nested is None:
            return None
        callee = self._chain_inner_callee(file_path, call, nested, caller_id, "rust")
        return None if callee is None else self._returned_type_id(callee, True)
