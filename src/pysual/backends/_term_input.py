"""Incremental terminal input decoding, independent of terminal I/O.

Escape sequences and UTF-8 can cross arbitrary reads. Probe responses are
returned separately; probing never consumes keystrokes or bracketed paste.
"""

import codecs
import re
import time

from ..host import Input, MAX_TEXT_BYTES


_CSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
_MOUSE = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
_ARROWS = {
    "A": "ArrowUp",
    "B": "ArrowDown",
    "C": "ArrowRight",
    "D": "ArrowLeft",
    "H": "Home",
    "F": "End",
    "P": "F1",
    "Q": "F2",
    "R": "F3",
    "S": "F4",
}
_TILDE = {
    1: "Home",
    2: "Insert",
    3: "Delete",
    4: "End",
    5: "PageUp",
    6: "PageDown",
    7: "Home",
    8: "End",
    11: "F1",
    12: "F2",
    13: "F3",
    14: "F4",
    15: "F5",
    17: "F6",
    18: "F7",
    19: "F8",
    20: "F9",
    21: "F10",
    23: "F11",
    24: "F12",
}
_CODES = {
    8: "Backspace",
    9: "Tab",
    10: "Enter",
    13: "Enter",
    27: "Escape",
    32: "Space",
    127: "Backspace",
    57348: "Insert",
    57349: "Delete",
    57350: "ArrowLeft",
    57351: "ArrowRight",
    57352: "ArrowUp",
    57353: "ArrowDown",
    57354: "PageUp",
    57355: "PageDown",
    57356: "Home",
    57357: "End",
}
_CODES.update({57364 + n: f"F{n + 1}" for n in range(35)})


class TerminalInput:
    """Decode VT, SGR mouse, xterm keys and the Kitty keyboard protocol."""

    def __init__(self, *, clock=time.monotonic):
        self.buffer = bytearray()
        self.replies: list[bytes] = []
        self.cell_width = 8.0
        self.cell_height = 16.0
        self.scale = 1.0
        self.pixel_mouse = False
        self._clock = clock
        self._escape_at = 0.0
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._paste: bytearray | None = None
        self._paste_overflow = False
        self._pointer = (0.0, 0.0)

    @staticmethod
    def _keys(key: str, *, shift=False, ctrl=False, event=0):
        # Legacy terminals have no release events. Pairing them makes button
        # activation and focus navigation behave like native host input.
        if ctrl and key.lower() == "q" and event != 3:
            return [Input("close")]
        if event == 3:
            return [Input("key_up", key=key, shift=shift, ctrl=ctrl)]
        result = [Input("key_down", key=key, shift=shift, ctrl=ctrl)]
        if event == 0:
            result.append(Input("key_up", key=key, shift=shift, ctrl=ctrl))
        return result

    def _plain(self, data):
        result = []
        for ch in self._decoder.decode(data):
            code = ord(ch)
            if ch in "\r\n\t\b\x7f":
                result.extend(self._keys(_CODES[code]))
            elif 0 < code < 27:
                result.extend(self._keys(chr(code + 96), ctrl=True))
            elif code >= 32:
                result.extend(
                    self._keys("Space" if ch == " " else ch.lower(), shift=ch.isupper())
                )
                result.append(Input("text", text=ch))
        return result

    def _sequence(self, sequence):
        mouse = _MOUSE.fullmatch(sequence)
        if mouse:
            code, raw_x, raw_y = map(int, mouse.group(1, 2, 3))
            # Character reports refer to cells; use the cell center. Pixel
            # reports are one-based device pixels, converted to logical units.
            if self.pixel_mouse:
                x, y = (raw_x - 1) / self.scale, (raw_y - 1) / self.scale
            else:
                x = (raw_x - 0.5) * self.cell_width / self.scale
                y = (raw_y - 0.5) * self.cell_height / self.scale
            self._pointer = (x, y)
            if code & 64:
                return [
                    Input(
                        "wheel",
                        x=x,
                        y=y,
                        delta=1 if code & 1 else -1,
                        shift=bool(code & (4 | 2)),
                        ctrl=bool(code & 16),
                    )
                ]
            kind = (
                "pointer_move"
                if code & 32
                else ("pointer_up" if mouse.group(4) == b"m" else "pointer_down")
            )
            return [
                Input(
                    kind,
                    x=x,
                    y=y,
                    shift=bool(code & 4),
                    ctrl=bool(code & 16),
                    button={0: 1, 1: 2, 2: 3}.get(code & 3, 1),
                )
            ]
        if sequence == b"\x1b[I":
            return [Input("repaint")]
        if sequence == b"\x1b[O":
            return [Input("blur")]
        if sequence.startswith((b"\x1b_G", b"\x1b]", b"\x1bP")) or (
            sequence.startswith(b"\x1b[?")
            or sequence.endswith(b"t")
            or sequence.endswith(b"c")
            or sequence.endswith(b"$y")
        ):
            self.replies.append(sequence)
            return []
        if sequence.startswith(b"\x1bO"):
            return self._keys(_ARROWS.get(chr(sequence[-1]), "Escape"))
        raw, final = sequence[2:-1].decode("ascii"), chr(sequence[-1])
        if final == "Z":
            return self._keys("Tab", shift=True)
        try:
            parts = raw.split(";")
            modifiers = int(parts[1].split(":")[0]) - 1 if len(parts) > 1 else 0
            event = (
                int(parts[1].split(":")[1]) if len(parts) > 1 and ":" in parts[1] else 0
            )
            common = dict(shift=bool(modifiers & 1), ctrl=bool(modifiers & (4 | 8)))
            if final == "u":
                code = int(parts[0].split(":")[0])
                if not 0 <= code <= 0x10FFFF or 0xD800 <= code <= 0xDFFF:
                    return []
                key = _CODES.get(code, chr(code).lower())
                result = self._keys(key, event=event, **common)
                if event != 3 and not common["ctrl"] and not modifiers & 2:
                    if len(parts) > 2 and parts[2]:
                        text = "".join(chr(int(n)) for n in parts[2].split(":"))
                        result.append(Input("text", text=text))
                    elif 32 <= code < 57344 or code > 63743:
                        shifted = parts[0].split(":")
                        value = (
                            chr(int(shifted[1]))
                            if common["shift"] and len(shifted) > 1 and shifted[1]
                            else chr(code)
                        )
                        result.append(
                            Input(
                                "text", text=value.upper() if common["shift"] else value
                            )
                        )
                return result
            if final in _ARROWS:
                return self._keys(_ARROWS[final], event=event, **common)
            if final == "~":
                code = int(parts[0])
                if code == 27 and len(parts) == 3:  # xterm modifyOtherKeys
                    return self._sequence(f"\x1b[{parts[2]};{parts[1]}u".encode())
                if code in _TILDE:
                    return self._keys(_TILDE[code], event=event, **common)
        except (ValueError, OverflowError):
            # Malformed/unrecognised controls must never become typed text.
            pass
        return []

    def feed(self, data=b"", *, flush=False):
        result = []
        if data:
            if not self.buffer:
                self._escape_at = self._clock()
            self.buffer.extend(data)
        while self.buffer:
            if self._paste is not None:
                end = self.buffer.find(b"\x1b[201~")
                count = end if end >= 0 else max(0, len(self.buffer) - 5)
                remaining = MAX_TEXT_BYTES - len(self._paste)
                self._paste.extend(self.buffer[: min(count, remaining)])
                self._paste_overflow |= count > remaining
                del self.buffer[:count]
                if end < 0:
                    break
                del self.buffer[:6]
                if self._paste_overflow:
                    result.append(
                        Input("resource_error", text="Terminal paste exceeds 8 MiB")
                    )
                else:
                    result.append(
                        Input(
                            "text",
                            text=self._paste.decode("utf-8", "replace")
                            .replace("\r\n", "\n")
                            .replace("\r", "\n"),
                            paste=True,
                        )
                    )
                self._paste = None
                continue
            if self.buffer[0] != 27:
                end = self.buffer.find(27)
                end = len(self.buffer) if end < 0 else end
                result.extend(self._plain(bytes(self.buffer[:end])))
                del self.buffer[:end]
                self._escape_at = self._clock()
                continue
            if self.buffer.startswith(b"\x1b[200~"):
                self._paste, self._paste_overflow = bytearray(), False
                del self.buffer[:6]
                continue
            if len(self.buffer) == 1:
                if flush or self._clock() - self._escape_at >= 0.035:
                    self.buffer.clear()
                    result.extend(self._keys("Escape"))
                break
            if self.buffer[1] in (ord("_"), ord("]"), ord("P")):
                end = self.buffer.find(b"\x1b\\", 2)
                bell = self.buffer.find(7, 2) if self.buffer[1] == ord("]") else -1
                if bell >= 0 and (end < 0 or bell < end):
                    end, extra = bell, 1
                else:
                    extra = 2
                if end < 0:
                    if len(self.buffer) > MAX_TEXT_BYTES * 2:
                        self.buffer.clear()
                    break
                sequence = bytes(self.buffer[: end + extra])
                del self.buffer[: end + extra]
                result.extend(self._sequence(sequence))
                continue
            if self.buffer[1] == ord("["):
                match = _CSI.match(self.buffer)
                if match is None:
                    if len(self.buffer) > 1024:
                        self.buffer.clear()
                    break
                sequence = match.group()
            elif self.buffer[1] == ord("O"):
                if len(self.buffer) < 3:
                    break
                sequence = bytes(self.buffer[:3])
            else:
                # Input has no Alt field. Preserve its key/text rather than
                # turning an Alt chord into an Escape action in a dialog.
                del self.buffer[0]
                continue
            del self.buffer[: len(sequence)]
            result.extend(self._sequence(sequence))
        return result
