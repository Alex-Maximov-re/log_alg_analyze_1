"""Parse Foresight/DebugView++ .dblog files for algorithm block and formula runs."""

from __future__ import annotations

import argparse
import json
import re
import sys
import csv
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator


GUID_RE = re.compile(r"^\[([0-9A-Fa-f]{16,})\]\s*")
ALG_ID_RE = re.compile(r"Получен ID алгоритма\s*-\s*(\S+)")
ALG_DONE_RE = re.compile(r"Время выполнения алгоритма:\s*(\S+)\s*=\s*(\d+)\s*мс")
HTTP_DURATION_RE = re.compile(r"^duration:\s*(\d+)\s*ms$", re.I)
RESULT_RE = re.compile(r'^result:\s*(\{.*\})\s*$')
ADDED_BLOCK_RE = re.compile(
    r"^Добавлен блок\s+(\S+)\s+(\S+)(?:\s+(\d+))?\s*$"
)
CALC_MODEL_RE = re.compile(r"^Расчет модели:\s*(\S+)\s*$")
MESSAGE_RE = re.compile(
    r'^Сообщение:\s*Расчёт модели "([^"]+)" в метамодели ""([^"]+)" '
    r"\((OBJ[^,\s]+),\s*(\d+)\)\"\s*$"
)
DIM_JSON_RE = re.compile(r'"dimension_values"\s*:\s*(\{.*\})\s*$')
DICT_MARK_RE = re.compile(
    r"Справочник\s+(\S+),\s*колво отметок\s*=\s*(\d+),\s*элемент\s*=\s*([^,]+),"
    r"\s*K?люч\s*-\s*([^,]*)\s*,\s*name\s*-\s*(.+)$"
)
DICT_MARK_OLD_RE = re.compile(
    r"Справочник\s+(\S+),\s*колво отметок\s*=\s*(\d+),\s*индекс элемента\s*=\s*([^,]+)\s*,"
    r"\s*name\s*-\s*(.+?),\s*Key\s*-\s*(.+)$"
)
CALLBACK_RE = re.compile(r"ЗАПУЩЕН КОЛБЭК КУБА\s*-\s*(.+?)\s*<<<<<")


def decode_dblog(raw: bytes) -> tuple[str, str]:
    """DebugView++ captures from Studio.exe are typically Windows-1251."""
    text_1251 = raw.decode("cp1251", errors="replace")
    if "DBG$" in text_1251 or "Расчет модели" in text_1251 or "Добавлен блок" in text_1251:
        return text_1251, "cp1251"
    try:
        text_utf8 = raw.decode("utf-8")
        return text_utf8, "utf-8"
    except UnicodeDecodeError:
        return text_1251, "cp1251"


def strip_payload(message: str) -> str | None:
    """Return the DBG$/DBG$ inner text, or None if the line is not a debug payload."""
    msg = message.strip()
    if not msg:
        return None
    if msg.startswith("$DBG:"):
        msg = msg[5:].lstrip()
    if not msg.startswith("DBG$:"):
        return None
    msg = msg[5:].lstrip()
    msg = GUID_RE.sub("", msg, count=1)
    return msg.strip()


def iter_debug_payloads(text: str) -> Iterator[tuple[str, str, str]]:
    """Yield (relative_time, timestamp, payload) for DBG$ lines."""
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        payload = strip_payload("\t".join(parts[4:]))
        if payload:
            yield parts[0], parts[1], payload


def parse_plan_result(result_obj: Any) -> list[dict[str, Any]]:
    raw = result_obj.get("result") if isinstance(result_obj, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return []
    entries: list[dict[str, Any]] = []
    for item in raw.split(";"):
        item = item.strip()
        if not item or "||" not in item:
            continue
        block_id, formula_id = item.split("||", 1)
        entries.append(
            {
                "order": len(entries) + 1,
                "block_id": block_id.strip(),
                "formula_id": formula_id.strip(),
            }
        )
    return entries


@dataclass
class ParseResult:
    source: str
    encoding: str
    algorithm_id: str | None = None
    cube_callback: str | None = None
    partial: bool = False
    algorithm_duration_ms: int | None = None
    http_duration_ms: int | None = None
    dimension_values: dict[str, Any] = field(default_factory=dict)
    slice_marks: list[dict[str, Any]] = field(default_factory=list)
    plan_raw: str | None = None
    plan: list[dict[str, Any]] = field(default_factory=list)
    added_blocks: list[dict[str, Any]] = field(default_factory=list)
    calc_model_events: int = 0
    message_events: int = 0
    # (object_id, name, instance_key, formula, local_id) -> count
    executions: dict[tuple[str, str, str, str, str | None], int] = field(
        default_factory=lambda: defaultdict(int)
    )
    calc_model_ids: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    other_messages: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _capture_slice: bool = True

    def to_dict(self) -> dict[str, Any]:
        metamodels: dict[tuple[str, str], dict[str, Any]] = {}
        for (object_id, name, instance_key, formula, local_id), count in self.executions.items():
            mk = (object_id, name)
            node = metamodels.setdefault(
                mk,
                {"object_id": object_id, "name": name, "instances": {}},
            )
            inst = node["instances"].setdefault(
                instance_key, {"instance_key": instance_key, "formulas": []}
            )
            inst["formulas"].append(
                {
                    "name": formula,
                    "local_id": local_id,
                    "count": count,
                }
            )

        metamodel_list = []
        for node in metamodels.values():
            instances = []
            for inst in node["instances"].values():
                inst["formulas"].sort(key=lambda x: (-x["count"], x["name"]))
                instances.append(inst)
            instances.sort(key=lambda x: x["instance_key"])
            metamodel_list.append(
                {
                    "object_id": node["object_id"],
                    "name": node["name"],
                    "instances": instances,
                }
            )
        metamodel_list.sort(key=lambda x: x["name"])

        unique_formulas = sum(
            len(inst["formulas"])
            for mm in metamodel_list
            for inst in mm["instances"]
        )

        return {
            "source": self.source,
            "encoding": self.encoding,
            "algorithm": {
                "id": self.algorithm_id,
                "cube_callback": self.cube_callback,
                "partial": self.partial,
                "duration_ms": self.algorithm_duration_ms,
                "dimension_values": self.dimension_values,
                "slice": self.slice_marks,
            },
            "plan": {
                "http_duration_ms": self.http_duration_ms,
                "raw": self.plan_raw,
                "entries": self.plan,
                "added_blocks": self.added_blocks,
            },
            "execution": {
                "calc_model_events": self.calc_model_events,
                "message_events": self.message_events,
                "unique_metamodels": len(metamodel_list),
                "unique_formula_rows": unique_formulas,
                "calc_model_ids": dict(
                    sorted(self.calc_model_ids.items(), key=lambda kv: (-kv[1], kv[0]))
                ),
                "metamodels": metamodel_list,
                "other_messages": [
                    {"text": text, "count": count}
                    for text, count in sorted(
                        self.other_messages.items(), key=lambda kv: (-kv[1], kv[0])
                    )
                ],
            },
        }


def parse_text(text: str, source: str, encoding: str) -> ParseResult:
    result = ParseResult(source=source, encoding=encoding)
    last_model_id: str | None = None

    for _rel, _ts, payload in iter_debug_payloads(text):
        if "Конец лога отметок" in payload:
            result._capture_slice = False
            continue

        if payload == ">>>> Запускаем частичный расчет <<<<":
            result.partial = True
            continue

        m = ALG_ID_RE.search(payload)
        if m:
            result.algorithm_id = m.group(1).rstrip("<").strip()
            continue

        m = ALG_DONE_RE.search(payload)
        if m:
            result.algorithm_id = result.algorithm_id or m.group(1)
            result.algorithm_duration_ms = int(m.group(2))
            continue

        m = CALLBACK_RE.search(payload)
        if m:
            result.cube_callback = m.group(1).strip()
            continue

        m = DIM_JSON_RE.search(payload)
        if m:
            try:
                result.dimension_values = json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
            continue

        m = DICT_MARK_RE.search(payload)
        if m and result._capture_slice:
            result.slice_marks.append(
                {
                    "dict": m.group(1),
                    "selected_count": int(m.group(2)),
                    "element_index": m.group(3).strip(),
                    "key": m.group(4).strip(),
                    "name": m.group(5).strip(),
                }
            )
            continue

        m = DICT_MARK_OLD_RE.search(payload)
        if m and result._capture_slice:
            result.slice_marks.append(
                {
                    "dict": m.group(1),
                    "selected_count": int(m.group(2)),
                    "element_index": m.group(3).strip(),
                    "key": m.group(5).strip(),
                    "name": m.group(4).strip(),
                }
            )
            continue

        m = RESULT_RE.match(payload)
        if m:
            result.plan_raw = m.group(1)
            try:
                result.plan = parse_plan_result(json.loads(m.group(1)))
            except json.JSONDecodeError:
                result.plan = []
            continue

        m = HTTP_DURATION_RE.match(payload)
        if m:
            result.http_duration_ms = int(m.group(1))
            continue

        m = ADDED_BLOCK_RE.match(payload)
        if m:
            result.added_blocks.append(
                {
                    "block_id": m.group(1),
                    "formula_id": m.group(2),
                    "order": int(m.group(3)) if m.group(3) else len(result.added_blocks) + 1,
                }
            )
            continue

        m = CALC_MODEL_RE.match(payload)
        if m:
            last_model_id = m.group(1)
            result.calc_model_events += 1
            result.calc_model_ids[last_model_id] += 1
            continue

        m = MESSAGE_RE.match(payload)
        if m:
            formula, name, object_id, instance_key = m.groups()
            result.message_events += 1
            key = (object_id, name, instance_key, formula, last_model_id)
            result.executions[key] += 1
            last_model_id = None
            continue

        if payload.startswith("Сообщение:"):
            result.other_messages[payload[len("Сообщение:") :].strip()] += 1
            continue

    return result


def parse_file(path: Path) -> ParseResult:
    raw = path.read_bytes()
    text, encoding = decode_dblog(raw)
    return parse_text(text, str(path), encoding)


def format_text_report(data: dict[str, Any]) -> str:
    alg = data["algorithm"]
    plan = data["plan"]
    exe = data["execution"]
    lines: list[str] = []
    lines.append(f"Источник: {data['source']}")
    lines.append(f"Кодировка: {data['encoding']}")
    lines.append("")
    lines.append("=== Алгоритм ===")
    lines.append(f"ID: {alg['id'] or '—'}")
    lines.append(f"Колбэк куба: {alg['cube_callback'] or '—'}")
    lines.append(f"Частичный расчёт: {'да' if alg['partial'] else 'нет / не залогировано'}")
    lines.append(f"Время выполнения (по DBG$): {alg['duration_ms'] if alg['duration_ms'] is not None else '—'} мс")
    if alg["slice"]:
        lines.append("Срез:")
        for mark in alg["slice"]:
            lines.append(
                f"  {mark['dict']}: {mark['name']} (index={mark['element_index']}, key={mark['key']})"
            )
    if alg["dimension_values"]:
        lines.append(f"dimension_values: {json.dumps(alg['dimension_values'], ensure_ascii=False)}")
    lines.append("")
    lines.append("=== План (ответ сервиса зависимостей) ===")
    lines.append(f"HTTP duration: {plan['http_duration_ms'] if plan['http_duration_ms'] is not None else '—'} ms")
    if not plan["entries"]:
        lines.append("Записей плана нет (в этом логе нет result: {block||formula}).")
    else:
        lines.append(f"Блоков/формул в плане: {len(plan['entries'])}")
        lines.append(f"{'№':<4} {'Блок':<32} {'Формула'}")
        for row in plan["entries"]:
            lines.append(f"{row['order']:<4} {row['block_id']:<32} {row['formula_id']}")
    lines.append("")
    lines.append("=== Исполнение (протокол расчёта модели) ===")
    lines.append(f"Событий «Расчет модели»: {exe['calc_model_events']}")
    lines.append(f"Событий «Сообщение»: {exe['message_events']}")
    lines.append(f"Уникальных метамоделей: {exe['unique_metamodels']}")
    lines.append(f"Уникальных строк формула x экземпляр: {exe['unique_formula_rows']}")
    if not exe["metamodels"]:
        lines.append("Именованных запусков формул нет.")
    else:
        for mm in exe["metamodels"]:
            lines.append("")
            lines.append(f"{mm['name']}")
            lines.append(f"  object_id: {mm['object_id']}")
            for inst in mm["instances"]:
                lines.append(f"  экземпляр key={inst['instance_key']}")
                for formula in inst["formulas"]:
                    lid = formula["local_id"] or "—"
                    lines.append(f"    {formula['name']}  [{lid}]  x{formula['count']}")
    if exe.get("other_messages"):
        lines.append("")
        lines.append("=== Прочие сообщения протокола ===")
        for row in exe["other_messages"]:
            lines.append(f"  {row['text']}  x{row['count']}")
    return "\n".join(lines)


def default_out_dir(log_path: Path) -> Path:
    return log_path.parent.parent / "output" if log_path.parent.name == "logs_for_analyze" else log_path.parent / "output"


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Извлечь из .dblog план блоков и фактически посчитанные формулы."
    )
    parser.add_argument("dblog", type=Path, help="Путь к файлу DebugView++ (.dblog)")
    parser.add_argument(
        "-o",
        "--out-dir",
        type=Path,
        default=None,
        help="Каталог для JSON и текстового отчёта (по умолчанию output/ рядом с проектом)",
    )
    parser.add_argument("--stdout", action="store_true", help="Печатать текстовый отчёт в stdout")
    args = parser.parse_args(list(argv) if argv is not None else None)

    log_path = args.dblog
    if not log_path.is_file():
        print(f"Файл не найден: {log_path}", file=sys.stderr)
        return 1

    parsed = parse_file(log_path)
    data = parsed.to_dict()
    report = format_text_report(data)

    out_dir = args.out_dir or default_out_dir(log_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = log_path.stem
    json_path = out_dir / f"{stem}.json"
    txt_path = out_dir / f"{stem}.txt"
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    txt_path.write_text(report + "\n", encoding="utf-8")

    csv_path = out_dir / f"{stem}_formulas.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(
            ["metamodel_name", "object_id", "instance_key", "formula_name", "local_id", "count"]
        )
        for mm in data["execution"]["metamodels"]:
            for inst in mm["instances"]:
                for formula in inst["formulas"]:
                    writer.writerow(
                        [
                            mm["name"],
                            mm["object_id"],
                            inst["instance_key"],
                            formula["name"],
                            formula["local_id"] or "",
                            formula["count"],
                        ]
                    )

    if args.stdout:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
        print(report)
    print(f"JSON: {json_path}")
    print(f"Отчёт: {txt_path}")
    print(f"CSV: {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
