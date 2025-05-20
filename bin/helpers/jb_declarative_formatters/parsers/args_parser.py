from __future__ import annotations


class ArgsParser:
    def __init__(self, expression: str, pos: int):
        self._expr = expression
        self._pos = pos
        self._expr_len = len(expression)

    def _current(self) -> str:
        assert self._pos < self._expr_len
        return self._expr[self._pos]

    def _next(self) -> None:
        self._pos += 1

    def _peek(self, next_offset: int = 1) -> str | None:
        pos_next = self._pos + next_offset
        if pos_next < self._expr_len:
            return self._expr[pos_next]
        return None

    def parse(self) -> tuple[list[str], int]:
        return self._parse_args_impl(')')

    def _parse_args_impl(self, exit_sym: str = None) -> tuple[list[str], int]:
        args = []
        arg_start = None

        while self._pos < self._expr_len:
            current = self._current()
            if current == '"':
                if arg_start is None:
                    arg_start = self._pos
                self.skip_string()
                continue

            elif current == '/' and self._peek() == '*':
                self.skip_comment()
                continue

            elif current == "'":
                self.skip_ansii_char()
                continue

            elif current == '(':
                if arg_start is None:
                    arg_start = self._pos
                self._next()
                self._parse_args_impl(')')
                continue

            elif current == ')' and exit_sym == current:
                if arg_start is not None:
                    args.append(self._expr[arg_start:self._pos])
                self._next()
                break
            elif current.isspace():
                self._next()
                continue
            elif current == ',':
                if arg_start is None:
                    args.append("")
                else:
                    args.append(self._expr[arg_start:self._pos])
                arg_start = None
                self._next()
                continue

            elif current == '{':
                if arg_start is None:
                    arg_start = self._pos
                self._next()
                self._parse_args_impl('}')
                continue
            elif current == '[':
                if arg_start is None:
                    arg_start = self._pos
                self._next()
                self._parse_args_impl(']')
                continue
            elif current in ']}' and exit_sym == current:
                self._next()
                break
            else:
                if arg_start is None:
                    arg_start = self._pos
                self._next()

        return args, self._pos

    def skip_string(self) -> None:
        assert self._current() == '"'
        self._next()

        while self._pos < self._expr_len and self._current() != '"':
            if self._current() == '\\':
                # skipping this and next symbol
                self._next()

            self._next()

        # last '"'
        self._next()

    def skip_comment(self):
        self._next()
        self._next()

        while self._current() != '*' or self._peek() != '/':
            self._next()

        self._next()
        self._next()

    # noinspection SpellCheckingInspection
    def skip_ansii_char(self):
        self._next()


def parse_args(expr: str, pos: int) -> tuple[list[str], int]:
    d = ArgsParser(expr, pos)
    return d.parse()
