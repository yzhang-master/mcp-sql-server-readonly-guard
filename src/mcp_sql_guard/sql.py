"""Tokenization and conservative SELECT-only SQL parsing."""

from __future__ import annotations

from dataclasses import dataclass


class SQLValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    position: int

    @property
    def upper(self) -> str:
        return self.value.upper()


@dataclass(frozen=True)
class SelectCore:
    tokens: tuple[Token, ...]


@dataclass(frozen=True)
class SelectBody:
    cores: tuple[SelectCore, ...]
    compounds: tuple[str, ...]


@dataclass(frozen=True)
class CTE:
    name: str
    body: Statement


@dataclass(frozen=True)
class Statement:
    body: SelectBody
    ctes: tuple[CTE, ...] = ()
    recursive: bool = False


_MULTI_CHAR_OPERATORS = ("||", "->>", "->", "<=", ">=", "<>", "!=", "==")
_SINGLE_CHAR = set("(),.;*+-/%=<>")
_COMPOUNDS = {"UNION", "INTERSECT", "EXCEPT"}
_NESTED_STARTS = {"SELECT", "WITH", "INSERT", "UPDATE", "DELETE", "REPLACE", "VALUES"}


def tokenize(sql: str) -> tuple[Token, ...]:
    tokens: list[Token] = []
    index = 0
    while index < len(sql):
        char = sql[index]
        if char.isspace():
            index += 1
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            index = len(sql) if newline < 0 else newline + 1
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end < 0:
                raise SQLValidationError("unterminated block comment")
            index = end + 2
            continue
        if char == "'":
            value, index = _quoted(sql, index, "'")
            tokens.append(Token("STRING", value, index - len(value)))
            continue
        if char in ('"', "`"):
            value, index = _quoted(sql, index, char)
            tokens.append(Token("IDENTIFIER", value, index - len(value)))
            continue
        if char == "[":
            end = sql.find("]", index + 1)
            if end < 0:
                raise SQLValidationError("unterminated quoted identifier")
            tokens.append(Token("IDENTIFIER", sql[index : end + 1], index))
            index = end + 1
            continue
        if (
            char in "xX"
            and index + 1 < len(sql)
            and sql[index + 1] == "'"
            and (not tokens or tokens[-1].position + len(tokens[-1].value) != index)
        ):
            value, end = _quoted(sql, index + 1, "'")
            tokens.append(Token("BLOB", sql[index:end], index))
            index = end
            continue
        if char.isdigit() or (char == "." and index + 1 < len(sql) and sql[index + 1].isdigit()):
            end = index + 1
            while end < len(sql) and (sql[end].isalnum() or sql[end] in ".+-"):
                if sql[end] in "+-" and sql[end - 1] not in "eE":
                    break
                end += 1
            tokens.append(Token("NUMBER", sql[index:end], index))
            index = end
            continue
        if char.isalpha() or char == "_":
            end = index + 1
            while end < len(sql) and (sql[end].isalnum() or sql[end] in "_$"):
                end += 1
            tokens.append(Token("WORD", sql[index:end], index))
            index = end
            continue
        if char in "?:@$":
            end = index + 1
            while end < len(sql) and (sql[end].isalnum() or sql[end] in "_$"):
                end += 1
            tokens.append(Token("PARAMETER", sql[index:end], index))
            index = end
            continue
        operator = next((op for op in _MULTI_CHAR_OPERATORS if sql.startswith(op, index)), None)
        if operator is not None:
            tokens.append(Token("OPERATOR", operator, index))
            index += len(operator)
            continue
        if char in _SINGLE_CHAR:
            kind = "PUNCTUATION" if char in "(),.;" else "OPERATOR"
            tokens.append(Token(kind, char, index))
            index += 1
            continue
        raise SQLValidationError(f"unsupported character {char!r} at position {index}")
    return tuple(tokens)


def _quoted(sql: str, start: int, quote: str) -> tuple[str, int]:
    index = start + 1
    while index < len(sql):
        if sql[index] == quote:
            if index + 1 < len(sql) and sql[index + 1] == quote:
                index += 2
                continue
            return sql[start : index + 1], index + 1
        index += 1
    raise SQLValidationError("unterminated quoted value")


class Parser:
    def __init__(self, tokens: tuple[Token, ...]):
        self.tokens = tokens
        self.index = 0

    def parse(self) -> Statement:
        if not self.tokens:
            raise SQLValidationError("empty SQL statement")
        statement = self._statement()
        if self._accept(";"):
            if self.index != len(self.tokens):
                raise SQLValidationError("multiple SQL statements are not allowed")
        if self.index != len(self.tokens):
            token = self.tokens[self.index]
            message = f"unexpected token {token.value!r} at position {token.position}"
            raise SQLValidationError(message)
        _validate_nested_queries(statement)
        return statement

    def _statement(self) -> Statement:
        if self._accept_word("WITH"):
            recursive = self._accept_word("RECURSIVE")
            ctes = [self._cte()]
            while self._accept(","):
                ctes.append(self._cte())
            if not self._peek_word("SELECT"):
                raise SQLValidationError("WITH must be followed by SELECT")
            return Statement(self._select_body(), tuple(ctes), recursive)
        if not self._peek_word("SELECT"):
            value = self.tokens[self.index].value if self.index < len(self.tokens) else "end"
            raise SQLValidationError(f"only SELECT statements are allowed, got {value!r}")
        return Statement(self._select_body())

    def _cte(self) -> CTE:
        name = self._take()
        if name.kind not in {"WORD", "IDENTIFIER"}:
            raise SQLValidationError("expected a CTE name")
        if self._accept("("):
            self._skip_group_body()
        if not self._accept_word("AS"):
            raise SQLValidationError("expected AS after CTE name")
        if self._accept_word("NOT") and not self._accept_word("MATERIALIZED"):
            raise SQLValidationError("expected MATERIALIZED after NOT")
        self._accept_word("MATERIALIZED")
        if not self._accept("("):
            raise SQLValidationError("expected parenthesized CTE query")
        body_tokens = self._skip_group_body()
        body = Parser(body_tokens).parse()
        return CTE(name.value, body)

    def _select_body(self) -> SelectBody:
        cores = [self._select_core()]
        compounds: list[str] = []
        while self.index < len(self.tokens) and self.tokens[self.index].upper in _COMPOUNDS:
            compounds.append(self._take().upper)
            if self._peek_word("ALL") or self._peek_word("DISTINCT"):
                self._take()
            cores.append(self._select_core())
        return SelectBody(tuple(cores), tuple(compounds))

    def _select_core(self) -> SelectCore:
        if not self._accept_word("SELECT"):
            raise SQLValidationError("compound query terms must be SELECT statements")
        start = self.index - 1
        depth = 0
        while self.index < len(self.tokens):
            token = self.tokens[self.index]
            if token.value == ";" and depth == 0:
                break
            if depth == 0 and token.upper in _COMPOUNDS:
                break
            if token.value == "(":
                depth += 1
            elif token.value == ")":
                depth -= 1
                if depth < 0:
                    raise SQLValidationError("unbalanced parentheses")
            self.index += 1
        if depth:
            raise SQLValidationError("unbalanced parentheses")
        return SelectCore(self.tokens[start : self.index])

    def _skip_group_body(self) -> tuple[Token, ...]:
        start = self.index
        depth = 1
        while self.index < len(self.tokens):
            token = self.tokens[self.index]
            if token.value == "(":
                depth += 1
            elif token.value == ")":
                depth -= 1
                if depth == 0:
                    body = self.tokens[start : self.index]
                    self.index += 1
                    return body
            self.index += 1
        raise SQLValidationError("unbalanced parentheses")

    def _take(self) -> Token:
        if self.index >= len(self.tokens):
            raise SQLValidationError("unexpected end of SQL statement")
        token = self.tokens[self.index]
        self.index += 1
        return token

    def _accept(self, value: str) -> bool:
        if self.index < len(self.tokens) and self.tokens[self.index].value == value:
            self.index += 1
            return True
        return False

    def _accept_word(self, value: str) -> bool:
        if self._peek_word(value):
            self.index += 1
            return True
        return False

    def _peek_word(self, value: str) -> bool:
        return (
            self.index < len(self.tokens)
            and self.tokens[self.index].kind == "WORD"
            and self.tokens[self.index].upper == value
        )


def parse_select(sql: str) -> Statement:
    return Parser(tokenize(sql)).parse()


def _validate_nested_queries(statement: Statement) -> None:
    for cte in statement.ctes:
        _validate_nested_queries(cte.body)
    for core in statement.body.cores:
        for index, token in enumerate(core.tokens):
            if token.value == ";":
                raise SQLValidationError("multiple SQL statements are not allowed")
            if token.value != "(":
                continue
            depth = 1
            end = index + 1
            while end < len(core.tokens) and depth:
                if core.tokens[end].value == "(":
                    depth += 1
                elif core.tokens[end].value == ")":
                    depth -= 1
                end += 1
            if depth:
                raise SQLValidationError("unbalanced parentheses")
            body = core.tokens[index + 1 : end - 1]
            if body and body[0].kind == "WORD" and body[0].upper in _NESTED_STARTS:
                if body[0].upper not in {"SELECT", "WITH"}:
                    raise SQLValidationError("non-SELECT subquery is not allowed")
                Parser(body).parse()
