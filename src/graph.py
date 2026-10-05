"""Knowledge Graph (Neo4j) + GraphRAG over two drug-topic knowledge bases.

Contract (fixed — bench_kg.py and the tests rely on it):
    link_entity(name, known)                       -> one of `known` or None          (TODO KG-1)
    build_graph(graph, law_docs, news_docs, llm_fn)   load both KBs into Neo4j      (TODO KG-2)
        every node created from ONE document carries the property `doc_id`
    Neo4jGraph.context(question, doc_ids)         -> list[str] facts               (TODO KG-3)
    GraphRAGAgent.answer(question, top_k)         -> str                           (TODO KG-4)

Everything else in this file is a HINT: one possible ontology (below). Use it as is, change it,
or design your own — your own ontology + report/ONTOLOGY.md earns the bonus (see SUBMISSION.md).

Suggested ontology (Crime is the bridge between the law KB and the news KB):

    (:Article {id, title, law, doc_id})-[:DEFINES]->(:Crime {name})
    (:Article)-[:HAS_CLAUSE]->(:Clause {id, number, penalty, text})-[:MENTIONS]->(:Substance {name})
    (:Case {name, summary, date, doc_id})-[:CHARGED_WITH]->(:Crime)
    (:Case)-[:INVOLVES {amount}]->(:Substance)
    (:Case)-[:LOCATED_IN]->(:Location {name})
    (:Person {name, aliases})-[:INVOLVED_IN {role, sentence, charge}]->(:Case)
"""

from __future__ import annotations

import difflib
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Callable

from .models import Document
from .store import EmbeddingStore

# Canonical substance names: the ones BLHS Chương XX lists, plus common ones in Vietnamese news.
SUBSTANCES = ["Heroine", "Cocaine", "Methamphetamine", "Amphetamine", "MDMA", "XLR-11", "Ketamine",
              "cần sa", "thuốc phiện", "côca"]
SUBSTANCE_ALIASES = {
    "ecstasy": "MDMA",
    "thuốc lắc": "MDMA",
    "heroin": "Heroine",
    "ma túy đá": "Methamphetamine",
    "meth": "Methamphetamine",
    "ketamin": "Ketamine",
    "cannabis": "cần sa",
    "marijuana": "cần sa",
}
CLAUSE_START = re.compile(r"^(\d+)\.\s", re.MULTILINE)
FOOTNOTE = re.compile(r"\[\d+\]")


def _substance_key(name: str) -> str:
    decomposed = unicodedata.normalize("NFD", name.strip().lower())
    without_marks = "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
    return re.sub(r"\s+", " ", without_marks)


def normalize_substance(name: str) -> str | None:
    """Return a canonical substance name for a corpus-backed spelling or alias."""
    by_key = {_substance_key(name): name for name in SUBSTANCES}
    by_key.update({_substance_key(alias): canonical for alias, canonical in SUBSTANCE_ALIASES.items()})
    return by_key.get(_substance_key(name))


def find_canonical_substances(text: str) -> list[str]:
    """Find canonical drug names and known aliases without partial-word matches."""
    normalized_text = _substance_key(text)
    names_by_key = {_substance_key(name): name for name in SUBSTANCES}
    aliases_by_key = {_substance_key(alias): canonical for alias, canonical in SUBSTANCE_ALIASES.items()}
    all_aliases = {**names_by_key, **aliases_by_key}
    found = set()
    for alias, canonical in all_aliases.items():
        if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", normalized_text):
            found.add(canonical)
    return [name for name in SUBSTANCES if name in found]


def _specific_drug_seed(question: str) -> str:
    """When a specific substance is named, avoid also seeding a generic `ma túy` node."""
    if not find_canonical_substances(question):
        return question
    return re.sub(r"\bma\s+t[uú]y\b(?!\s+đá)", " ", question, flags=re.IGNORECASE)


_QUANTITY_PATTERN = re.compile(
    r"(?:khối lượng|thể tích)\s+(?:từ\s+)?"
    r"(?P<lower>\d+(?:[.,]\d+)*)\s*(?P<unit>kilôgam|kilogram|kg|gam|grams?|g|mililít|milliliters?|ml|lít|liters?|l)"
    r"(?:\s+(?P<range>đến(?:\s+dưới)?)\s+(?P<upper>\d+(?:[.,]\d+)*)\s*"
    r"(?P<upper_unit>kilôgam|kilogram|kg|gam|grams?|g|mililít|milliliters?|ml|lít|liters?|l))?",
    re.IGNORECASE,
)


def _parse_vietnamese_number(value: str) -> float:
    if "," in value:
        return float(value.replace(".", "").replace(",", "."))
    pieces = value.split(".")
    if len(pieces) > 1 and len(pieces[-1]) == 3:
        return float("".join(pieces))
    return float(value)


def _canonical_quantity(value: str, unit: str) -> tuple[float, str]:
    amount = _parse_vietnamese_number(value)
    normalized_unit = unit.lower().replace("ô", "o")
    if normalized_unit in {"kilogam", "kilogram", "kg"}:
        return amount * 1000, "g"
    if normalized_unit in {"gam", "gram", "grams", "g"}:
        return amount, "g"
    if normalized_unit in {"lit", "liter", "liters", "l"}:
        return amount * 1000, "ml"
    return amount, "ml"


def _parse_condition_quantity(text: str) -> dict[str, Any]:
    match = _QUANTITY_PATTERN.search(text)
    if not match:
        return {"min_amount": None, "max_amount": None, "unit": "", "max_exclusive": False}

    min_amount, unit = _canonical_quantity(match.group("lower"), match.group("unit"))
    max_amount = None
    max_exclusive = False
    if match.group("upper"):
        upper_amount, upper_unit = _canonical_quantity(match.group("upper"), match.group("upper_unit"))
        if upper_unit == unit:
            max_amount = upper_amount
            max_exclusive = match.group("range").lower() == "đến dưới"
        else:
            return {"min_amount": None, "max_amount": None, "unit": "", "max_exclusive": False}
    return {"min_amount": min_amount, "max_amount": max_amount, "unit": unit,
            "max_exclusive": max_exclusive}


def parse_custom_law_article(doc: Document) -> dict[str, Any]:
    """Parse an Article, its Provisions and point-level PenaltyConditions deterministically."""
    article_id = doc.metadata.get("article", doc.id)
    title = doc.metadata.get("title", "").split(". ", 1)[-1]
    body = FOOTNOTE.sub("", doc.content)
    starts = list(CLAUSE_START.finditer(body))
    provisions = []

    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
        clause_text = body[start.start():end].strip()
        clause_number = int(start.group(1))
        first_line = clause_text.splitlines()[0]
        penalty_match = re.search(r"\bbị ((?:phạt|tù|cảnh cáo).+?)(?::|$)", first_line, re.IGNORECASE)
        provision_id = f"{article_id} khoản {clause_number}"
        provision = {
            "id": provision_id,
            "article_id": article_id,
            "number": clause_number,
            "penalty": penalty_match.group(1).rstrip(".") if penalty_match else "",
            "text": clause_text,
            "doc_id": doc.id,
            "conditions": [],
        }

        point_starts = list(re.finditer(r"(?m)^([a-zđ])\)\s*", clause_text, re.IGNORECASE))
        for point_index, point_start in enumerate(point_starts):
            point_end = point_starts[point_index + 1].start() if point_index + 1 < len(point_starts) else len(clause_text)
            condition_text = clause_text[point_start.start():point_end].strip()
            point = point_start.group(1).lower()
            quantity = _parse_condition_quantity(condition_text)
            provision["conditions"].append({
                "id": f"{provision_id} điểm {point}",
                "article_id": article_id,
                "provision_id": provision_id,
                "point": point,
                "text": condition_text,
                "substances": find_canonical_substances(condition_text),
                "doc_id": doc.id,
                **quantity,
            })
        provisions.append(provision)

    return {
        "id": article_id,
        "title": title,
        "law": doc.metadata.get("law", ""),
        "doc_id": doc.id,
        "crime": normalize_crime(title) if title.startswith("Tội ") else None,
        "provisions": provisions,
    }


def parse_statutory_definitions(doc: Document) -> list[dict[str, Any]]:
    """Extract numbered term definitions from the PCMT law's glossary Article."""
    if "PCMT" not in doc.metadata.get("law", "") or "giải thích từ ngữ" not in doc.metadata.get("title", "").lower():
        return []

    starts = list(CLAUSE_START.finditer(doc.content))
    definitions = []
    article_id = doc.metadata.get("article", doc.id)
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(doc.content)
        entry = doc.content[start.start():end].strip()
        entry = re.sub(r"^\d+\.\s*", "", entry)
        parts = re.split(r"\s+là\s+", entry, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) != 2:
            continue
        term, definition = (re.sub(r"\s+", " ", part).strip() for part in parts)
        if not term or not definition:
            continue
        definitions.append({
            "id": f"{article_id} định nghĩa {_substance_key(term)}",
            "article_id": article_id,
            "term": term,
            "text": definition,
            "doc_id": doc.id,
        })
    return definitions

def load_markdown_docs(folder: str | Path) -> list[Document]:
    """Read crawler output (.md with a flat `key: "value"` front matter) into Documents."""
    docs = []
    for path in sorted(Path(folder).glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        _, front, body = raw.split("---", 2)
        metadata = {k: json.loads(v) for k, v in re.findall(r'^(\w+): (".*")$', front, re.MULTILINE)}
        docs.append(Document(id=metadata.get("doc_id", path.stem), content=body.strip(), metadata=metadata))
    return docs

def normalize_crime(name: str) -> str:
    """'Tội Mua bán trái phép chất ma túy' -> 'mua bán trái phép chất ma túy'."""
    name = re.sub(r"\s+", " ", name.strip().strip("\"'“”").lower())
    return name.removeprefix("tội ").strip()

def link_entity(name: str, known: list[str], normalize: Callable[[str], str] = normalize_crime) -> str | None:
    """Map a free-text mention (e.g. a charge written by a journalist) onto one canonical name in `known`."""
    normalized = normalize(name)
    if not normalized:
        return None

    original_by_normalized: dict[str, str] = {}
    for candidate in known:
        key = normalize(candidate)
        if key:
            original_by_normalized.setdefault(key, candidate)

    if normalized in original_by_normalized:
        return original_by_normalized[normalized]

    matches = difflib.get_close_matches(normalized, list(original_by_normalized), n=1, cutoff=0.8)
    return original_by_normalized[matches[0]] if matches else None

def find_substances(text: str) -> list[str]:
    lowered = text.lower()
    return [name for name in SUBSTANCES if name.lower() in lowered]

# ----------------------------------------------------------------------------------------------
# HINT — suggested ontology: extraction helpers
# ----------------------------------------------------------------------------------------------

def parse_law_article(doc: Document) -> dict[str, Any]:
    """Deterministic (regex) extraction for one 'Điều' — law text is regular enough to skip the LLM."""
    article_id = doc.metadata["article"]                       # "Điều 251 BLHS"
    title = doc.metadata["title"].split(". ", 1)[-1]           # "Tội mua bán trái phép chất ma túy"
    body = FOOTNOTE.sub("", doc.content)
    starts = list(CLAUSE_START.finditer(body))
    clauses = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
        text = body[start.start():end].strip()
        first_line = text.splitlines()[0]
        penalty = re.search(r"\bbị ((?:phạt|tù|cảnh cáo).+?)(?::|$)", first_line)
        clauses.append({
            "id": f"{article_id} khoản {start.group(1)}",
            "number": int(start.group(1)),
            "penalty": penalty.group(1).rstrip(".") if penalty else "",
            "text": text,
            "substances": find_substances(text),
        })
    return {
        "id": article_id,
        "law": doc.metadata.get("law", ""),
        "title": title,
        "doc_id": doc.id,
        "crime": normalize_crime(title) if title.startswith("Tội ") else None,
        "clauses": clauses,
    }

NEWS_EXTRACTION_PROMPT = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin có trong bài. Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ mua bán 36kg ma túy tại TP.HCM",
  "summary": "1-2 câu tóm tắt",
  "date": "ngày xảy ra/xét xử nếu có, dạng YYYY-MM-DD hoặc chuỗi rỗng",
  "location": "tỉnh/thành phố, chuỗi rỗng nếu không rõ",
  "charges": ["tội danh, BẮT BUỘC chọn đúng nguyên văn từ DANH SÁCH TỘI DANH"],
  "substances": [{{"name": "tên chất, dùng tên chuẩn trong DANH SÁCH CHẤT nếu khớp", "amount": "khối lượng nếu có"}}],
  "people": [{{"name": "họ tên", "aliases": ["biệt danh"], "role": "bị cáo|bị can|nghi phạm|người liên quan|cán bộ",
               "charge": "tội danh của người này (từ DANH SÁCH TỘI DANH) hoặc chuỗi rỗng",
               "sentence": "mức án nếu có, ví dụ: tử hình, 8 năm tù"}}]
}}]}}
Bài không nói về vụ việc cụ thể (tuyên truyền, hội nghị...) thì trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""

def extract_news_cases(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    """LLM extraction for one news article; charges are re-linked to law-KB crimes in code."""
    prompt = NEWS_EXTRACTION_PROMPT.format(
        crimes="; ".join(known_crimes), substances=", ".join(SUBSTANCES),
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    try:
        response = llm_fn(prompt)
        if not isinstance(response, str):
            return []
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", response.strip(), re.IGNORECASE | re.DOTALL)
        payload = json.loads(fenced.group(1) if fenced else response)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        return []
    cases = [case for case in payload["cases"] if isinstance(case, dict)]
    for case in cases:
        charges = case.get("charges", [])
        if not isinstance(charges, list):
            charges = []
        case["charges"] = sorted({c for c in (link_entity(x, known_crimes) for x in charges if isinstance(x, str)) if c})
        people = case.get("people", [])
        if not isinstance(people, list):
            people = []
        case["people"] = [person for person in people if isinstance(person, dict)]
        for person in case["people"]:
            person_charge = person.get("charge") or ""
            person["charge"] = link_entity(person_charge, known_crimes) or "" if isinstance(person_charge, str) else ""
    return cases


CUSTOM_NEWS_EXTRACTION_PROMPT = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin bài báo nêu rõ. Trả về JSON theo đúng schema:
{{"cases": [{{
  "name": "tên mô tả vụ việc",
  "summary": "tóm tắt ngắn",
  "date": "ngày xảy ra hoặc xét xử nếu có",
  "stage": "giai đoạn tố tụng được nêu, hoặc chuỗi rỗng",
  "location": "địa điểm được nêu, hoặc chuỗi rỗng",
  "charges": ["chọn tên trong DANH SÁCH TỘI DANH nếu bài nêu rõ"],
  "substances": [{{"name": "tên chất", "amount": "khối lượng nguyên văn nếu có"}}],
  "people": [{{"name": "họ tên", "aliases": ["biệt danh"], "role": "vai trò",
                "charge": "tội danh của người này nếu có, nếu không để rỗng",
                "sentence": "mức án nếu có, nếu không để rỗng", "stage": "giai đoạn liên quan nếu có"}}]
}}]}}
Nếu bài không mô tả vụ việc cụ thể, trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT CHUẨN: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""


def _person_key(name: str) -> str:
    """Stable identity key for a person's full name, ignoring accents and punctuation."""
    return re.sub(r"[^\w\s]", "", _substance_key(name), flags=re.UNICODE).strip()


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(re.sub(r"\s+", " ", item).strip() for item in value
                               if isinstance(item, str) and item.strip()))


def _normalize_news_substance(name: str) -> str:
    canonical = normalize_substance(name)
    if canonical:
        return canonical
    matches = find_canonical_substances(name)
    if len(matches) == 1:
        return matches[0]
    return re.sub(r"\s+", " ", name.strip())


def _parse_reported_amount(raw_amount: str) -> tuple[float | None, str]:
    match = re.search(
        r"(?P<amount>\d+(?:[.,]\d+)*)\s*(?P<unit>kilôgam|kilogram|kg|gam|grams?|g|mililít|milliliters?|ml|lít|liters?|l)(?!\w)",
        raw_amount,
        re.IGNORECASE,
    )
    if not match:
        return None, ""
    try:
        return _canonical_quantity(match.group("amount"), match.group("unit"))
    except ValueError:
        return None, ""


def extract_custom_news_cases(doc: Document, llm_fn: Callable[..., str], known_crimes: list[str]) -> list[dict[str, Any]]:
    """Extract validated news entities and canonicalize identity, charges, substances and amounts."""
    prompt = CUSTOM_NEWS_EXTRACTION_PROMPT.format(
        crimes="; ".join(known_crimes),
        substances=", ".join(SUBSTANCES) + "; aliases: " + ", ".join(SUBSTANCE_ALIASES),
        title=doc.metadata.get("title", ""),
        content=doc.content[:12000],
    )
    try:
        response = llm_fn(prompt, json_mode=True)
        payload = json.loads(response) if isinstance(response, str) else response
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        return []

    results = []
    source_title = doc.metadata.get("title", doc.id)
    for raw_case in payload["cases"]:
        if not isinstance(raw_case, dict):
            continue
        raw_charges = _string_list(raw_case.get("charges"))
        charges = list(dict.fromkeys(linked for linked in
                                     (link_entity(charge, known_crimes) for charge in raw_charges) if linked))
        case_stage = raw_case.get("stage", "") if isinstance(raw_case.get("stage", ""), str) else ""

        substances = []
        raw_substances = raw_case.get("substances", [])
        if isinstance(raw_substances, list):
            for raw_substance in raw_substances:
                if isinstance(raw_substance, str):
                    raw_substance = {"name": raw_substance}
                if not isinstance(raw_substance, dict) or not isinstance(raw_substance.get("name"), str):
                    continue
                original_name = re.sub(r"\s+", " ", raw_substance["name"]).strip()
                if not original_name:
                    continue
                name = _normalize_news_substance(original_name)
                amount = raw_substance.get("amount", "")
                amount = re.sub(r"\s+", " ", amount).strip() if isinstance(amount, str) else ""
                amount_value, unit = _parse_reported_amount(amount)
                aliases = [original_name] if original_name != name else []
                substances.append({
                    "name": name,
                    "aliases": aliases,
                    "raw_amount": amount,
                    "amount_value": amount_value,
                    "unit": unit,
                    "source_doc_ids": [doc.id],
                })

        people = []
        raw_people = raw_case.get("people", [])
        if isinstance(raw_people, list):
            for raw_person in raw_people:
                if not isinstance(raw_person, dict) or not isinstance(raw_person.get("name"), str):
                    continue
                name = re.sub(r"\s+", " ", raw_person["name"]).strip()
                person_id = _person_key(name)
                if not person_id:
                    continue
                person_charge = raw_person.get("charge", "")
                person_charge = person_charge if isinstance(person_charge, str) else ""
                person_charge = link_entity(person_charge, known_crimes) or ""
                if not person_charge and len(charges) == 1:
                    person_charge = charges[0]
                aliases = _string_list(raw_person.get("aliases"))
                stage = raw_person.get("stage", "") if isinstance(raw_person.get("stage", ""), str) else ""
                people.append({
                    "id": person_id,
                    "name": name,
                    "aliases": aliases,
                    "role": raw_person.get("role", "") if isinstance(raw_person.get("role", ""), str) else "",
                    "charge": person_charge,
                    "sentence": raw_person.get("sentence", "") if isinstance(raw_person.get("sentence", ""), str) else "",
                    "stage": stage or case_stage,
                    "source_doc_ids": [doc.id],
                })

        name = raw_case.get("name", "")
        name = re.sub(r"\s+", " ", name).strip() if isinstance(name, str) else ""
        summary = raw_case.get("summary", "")
        summary = re.sub(r"\s+", " ", summary).strip() if isinstance(summary, str) else ""
        date = raw_case.get("date", "") if isinstance(raw_case.get("date", ""), str) else ""
        location = raw_case.get("location", "") if isinstance(raw_case.get("location", ""), str) else ""
        location = re.sub(r"\s+", " ", location).strip()
        results.append({
            "name": name or source_title,
            "summary": summary,
            "date": date.strip(),
            "stage": case_stage,
            "location": location,
            "charges": charges,
            "substances": substances,
            "people": people,
            "source_title": source_title,
            "doc_id": doc.id,
        })
    return results

# ----------------------------------------------------------------------------------------------
# Neo4j
# ----------------------------------------------------------------------------------------------

class Neo4jGraph:
    """Thin wrapper over the official neo4j driver."""

    def __init__(self, uri: str, user: str, password: str) -> None:
        from neo4j import GraphDatabase

        self.driver = GraphDatabase.driver(uri, auth=(user, password), notifications_min_severity="OFF")
        self.driver.verify_connectivity()

    def close(self) -> None:
        self.driver.close()

    def run(self, cypher: str, **params: Any) -> list[dict]:
        records, _, _ = self.driver.execute_query(cypher, params)
        return [record.data() for record in records]

    def reset(self) -> None:
        """Delete every node, relationship and constraint (bench_kg.py calls this before build_graph)."""
        self.run("MATCH (n) DETACH DELETE n")
        for row in self.run("SHOW CONSTRAINTS YIELD name RETURN name"):
            self.run(f"DROP CONSTRAINT `{row['name']}` IF EXISTS")

    def stats(self) -> dict[str, int]:
        nodes = self.run("MATCH (n) RETURN count(n) AS n")[0]["n"]
        rels = self.run("MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"]
        return {"nodes": nodes, "relationships": rels}

    def seed_facts(self, question: str, doc_ids: list[str], skip_labels: tuple[str, ...] = (),
                   limit: int = 60) -> tuple[list[str], list[str]]:
        """Ontology-independent first step: seed nodes + their 1-hop edges as text facts.

        Seeds = nodes whose `doc_id` is in doc_ids, or whose `name`/`aliases` appear in the question.
        Returns (seed elementIds, facts). Nodes with a label in skip_labels are left out of the facts.
        """
        seeds = self.run(
            """
            MATCH (n)
            WHERE n.doc_id IN $doc_ids
               OR (n.name IS :: STRING AND size(n.name) >= 3 AND toLower($q) CONTAINS toLower(n.name))
               OR any(a IN coalesce(n.aliases, []) WHERE size(a) >= 3 AND toLower($q) CONTAINS toLower(a))
            RETURN elementId(n) AS id
            """,
            q=question, doc_ids=doc_ids,
        )
        seed_ids = [row["id"] for row in seeds]
        edges = self.run(
            """
            MATCH (s)-[r]-(m)
            WHERE elementId(s) IN $ids
              AND none(l IN labels(s) + labels(m) WHERE l IN $skip)
            WITH DISTINCT r LIMIT $limit
            WITH startNode(r) AS a, r, endNode(r) AS b
            RETURN labels(a)[0] AS a_label, coalesce(a.name, a.id) AS a_name, type(r) AS rel,
                   properties(r) AS props, labels(b)[0] AS b_label, coalesce(b.name, b.id) AS b_name
            """,
            ids=seed_ids, skip=list(skip_labels), limit=limit,
        )
        facts = []
        for e in edges:
            props = ", ".join(f"{k}: {v}" for k, v in e["props"].items() if v)
            facts.append(f"({e['a_label']}: {e['a_name']}) -[{e['rel']}{' {' + props + '}' if props else ''}]-> "
                         f"({e['b_label']}: {e['b_name']})")
        return seed_ids, facts

    # ---------------------------------------------------------------- HINT — suggested ontology: writes

    def custom_constraints(self) -> None:
        """Enforce the custom ontology's canonical and document-scoped identities."""
        for label, key in [
            ("Article", "id"), ("Provision", "id"), ("PenaltyCondition", "id"),
            ("Definition", "id"), ("Crime", "name"), ("Case", "id"),
            ("Substance", "name"), ("Person", "id"), ("Location", "id"),
        ]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_custom_law(self, articles: list[dict[str, Any]], definitions: list[dict[str, Any]]) -> None:
        """Write parsed law records, retaining source IDs on document and canonical nodes."""
        if articles:
            self.run(
                """
                UNWIND $articles AS item
                MERGE (a:Article {id: item.id})
                SET a.title = item.title, a.law = item.law, a.doc_id = item.doc_id
                FOREACH (crime_name IN CASE WHEN item.crime IS NULL THEN [] ELSE [item.crime] END |
                    MERGE (c:Crime {name: crime_name})
                    ON CREATE SET c.doc_id = item.doc_id, c.source_doc_ids = [item.doc_id]
                    SET c.source_doc_ids = CASE WHEN item.doc_id IN coalesce(c.source_doc_ids, [])
                        THEN coalesce(c.source_doc_ids, []) ELSE coalesce(c.source_doc_ids, []) + item.doc_id END
                    MERGE (a)-[r:DEFINES]->(c) SET r.source_doc_id = item.doc_id
                )
                """,
                articles=articles,
            )

        provisions = [provision for article in articles for provision in article["provisions"]]
        if provisions:
            self.run(
                """
                UNWIND $provisions AS item
                MATCH (a:Article {id: item.article_id})
                MERGE (v:Provision {id: item.id})
                SET v.number = item.number, v.penalty = item.penalty, v.text = item.text, v.doc_id = item.doc_id
                MERGE (a)-[r:HAS_PROVISION]->(v) SET r.source_doc_id = item.doc_id
                """,
                provisions=provisions,
            )

        conditions = [condition for provision in provisions for condition in provision["conditions"]]
        if conditions:
            self.run(
                """
                UNWIND $conditions AS item
                MATCH (v:Provision {id: item.provision_id})
                MERGE (t:PenaltyCondition {id: item.id})
                SET t.point = item.point, t.text = item.text, t.min_amount = item.min_amount,
                    t.max_amount = item.max_amount, t.unit = item.unit,
                    t.max_exclusive = item.max_exclusive, t.doc_id = item.doc_id
                MERGE (v)-[r:HAS_CONDITION]->(t) SET r.source_doc_id = item.doc_id
                FOREACH (substance_name IN item.substances |
                    MERGE (s:Substance {name: substance_name})
                    ON CREATE SET s.doc_id = item.doc_id, s.source_doc_ids = [item.doc_id], s.aliases = []
                    SET s.source_doc_ids = CASE WHEN item.doc_id IN coalesce(s.source_doc_ids, [])
                        THEN coalesce(s.source_doc_ids, []) ELSE coalesce(s.source_doc_ids, []) + item.doc_id END
                    MERGE (t)-[ap:APPLIES_TO]->(s) SET ap.source_doc_id = item.doc_id
                )
                """,
                conditions=conditions,
            )

        if definitions:
            self.run(
                """
                UNWIND $definitions AS item
                MATCH (a:Article {id: item.article_id})
                MERGE (d:Definition {id: item.id})
                SET d.term = item.term, d.text = item.text, d.doc_id = item.doc_id
                MERGE (a)-[r:HAS_DEFINITION]->(d) SET r.source_doc_id = item.doc_id
                """,
                definitions=definitions,
            )

    def add_custom_news_cases(self, cases: list[dict[str, Any]]) -> None:
        """Write validated news cases in one parameterized batch."""
        if not cases:
            return
        self.run(
            """
            UNWIND $cases AS item
            MERGE (k:Case {id: item.id})
            SET k.name = item.name, k.summary = item.summary, k.date = item.date,
                k.stage = item.stage, k.doc_id = item.doc_id, k.source_title = item.source_title
            FOREACH (crime_name IN item.charges |
                MERGE (c:Crime {name: crime_name})
                ON CREATE SET c.doc_id = item.doc_id, c.source_doc_ids = [item.doc_id]
                SET c.source_doc_ids = CASE WHEN item.doc_id IN coalesce(c.source_doc_ids, [])
                    THEN coalesce(c.source_doc_ids, []) ELSE coalesce(c.source_doc_ids, []) + item.doc_id END
                MERGE (k)-[r:CHARGED_WITH]->(c) SET r.source_doc_id = item.doc_id
            )
            FOREACH (s IN item.substances |
                MERGE (sub:Substance {name: s.name})
                ON CREATE SET sub.doc_id = item.doc_id, sub.source_doc_ids = [item.doc_id], sub.aliases = []
                SET sub.source_doc_ids = CASE WHEN item.doc_id IN coalesce(sub.source_doc_ids, [])
                    THEN coalesce(sub.source_doc_ids, []) ELSE coalesce(sub.source_doc_ids, []) + item.doc_id END
                SET sub.aliases = reduce(acc = coalesce(sub.aliases, []), alias IN s.aliases |
                    CASE WHEN alias IN acc THEN acc ELSE acc + alias END)
                MERGE (k)-[r:INVOLVES]->(sub)
                SET r.raw_amount = s.raw_amount, r.amount_value = s.amount_value,
                    r.unit = s.unit, r.source_doc_id = item.doc_id
            )
            FOREACH (p IN item.people |
                MERGE (person:Person {id: p.id})
                ON CREATE SET person.name = p.name, person.doc_id = item.doc_id,
                    person.source_doc_ids = [item.doc_id], person.aliases = []
                SET person.source_doc_ids = CASE WHEN item.doc_id IN coalesce(person.source_doc_ids, [])
                    THEN coalesce(person.source_doc_ids, []) ELSE coalesce(person.source_doc_ids, []) + item.doc_id END
                SET person.aliases = reduce(acc = coalesce(person.aliases, []), alias IN p.aliases |
                    CASE WHEN alias IN acc THEN acc ELSE acc + alias END)
                MERGE (person)-[r:PARTICIPATED_IN]->(k)
                SET r.role = p.role, r.charge = p.charge, r.sentence = p.sentence,
                    r.stage = p.stage, r.source_doc_id = item.doc_id
                SET r.source_doc_ids = CASE WHEN item.doc_id IN coalesce(r.source_doc_ids, [])
                    THEN coalesce(r.source_doc_ids, []) ELSE coalesce(r.source_doc_ids, []) + item.doc_id END
            )
            FOREACH (location IN CASE WHEN item.location = '' THEN [] ELSE [item.location] END |
                MERGE (loc:Location {id: item.location_id})
                ON CREATE SET loc.name = location, loc.doc_id = item.doc_id, loc.source_doc_ids = [item.doc_id]
                SET loc.source_doc_ids = CASE WHEN item.doc_id IN coalesce(loc.source_doc_ids, [])
                    THEN coalesce(loc.source_doc_ids, []) ELSE coalesce(loc.source_doc_ids, []) + item.doc_id END
                MERGE (k)-[r:LOCATED_IN]->(loc) SET r.source_doc_id = item.doc_id
            )
            """,
            cases=cases,
        )

    def suggested_constraints(self) -> None:
        for label, key in [("Article", "id"), ("Clause", "id"), ("Crime", "name"), ("Case", "name"),
                           ("Substance", "name"), ("Person", "name"), ("Location", "name")]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_law_article(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id}) SET a.title = $title, a.law = $law, a.doc_id = $doc_id
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime}) MERGE (a)-[:DEFINES]->(c))
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.penalty = clause.penalty, cl.text = clause.text, cl.doc_id = $doc_id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (s IN clause.substances | MERGE (sub:Substance {name: s}) MERGE (cl)-[:MENTIONS]->(sub))
            """,
            **article,
        )

    def add_news_case(self, case: dict, doc: Document) -> None:
        self.run(
            """
            MERGE (k:Case {name: $name})
              SET k.summary = $summary, k.date = $date, k.doc_id = $doc_id, k.source_title = $title
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: loc}) MERGE (k)-[:LOCATED_IN]->(l))
            FOREACH (crime IN $charges | MERGE (c:Crime {name: crime}) MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances | MERGE (sub:Substance {name: s.name}) MERGE (k)-[r:INVOLVES]->(sub)
                SET r.amount = s.amount)
            FOREACH (p IN $people | MERGE (person:Person {name: p.name})
                SET person.aliases = coalesce(p.aliases, [])
                MERGE (person)-[r:INVOLVED_IN]->(k) SET r.role = p.role, r.charge = p.charge, r.sentence = p.sentence)
            """,
            name=case.get("name") or doc.metadata.get("title", doc.id),
            summary=case.get("summary", ""), date=case.get("date", ""), location=case.get("location", ""),
            charges=case.get("charges", []), people=[p for p in case.get("people", []) if p.get("name")],
            substances=[s for s in case.get("substances", []) if s.get("name")],
            doc_id=doc.id, title=doc.metadata.get("title", ""),
        )

    # ---------------------------------------------------------------- KG-3

    def context(self, question: str, doc_ids: list[str], max_facts: int = 60) -> list[str]:
        """Return bounded facts from the selected ontology, expanding news seeds into statute."""
        if max_facts <= 0:
            return []
        ontology = os.getenv("DRUG_KG_ONTOLOGY", "custom").strip().lower()
        if ontology == "hint":
            return self._hint_context(question, doc_ids, max_facts)
        if ontology != "custom":
            raise ValueError(f"Unknown DRUG_KG_ONTOLOGY={ontology!r}; expected 'custom' or 'hint'.")
        return self._custom_context(question, doc_ids, max_facts)

    @staticmethod
    def _legal_facts(rows: list[dict], question: str, limit: int, hint: bool = False) -> list[str]:
        """Render one selected penalty provision per matched article and case."""
        if limit <= 0:
            return []
        grouped: dict[tuple[str, str], list[dict]] = {}
        for row in rows:
            if row.get("article_id") and row.get("provision_number") is not None:
                grouped.setdefault((row.get("case_id", ""), row["article_id"]), []).append(row)

        lowered = question.lower()
        article_numbers = re.findall(r"[đd]iều\s+(\d+)", question, re.IGNORECASE)
        wants_maximum = any(word in lowered for word in ("tối đa", "cao nhất", "lớn nhất", "maximum"))
        wants_basic = any(word in lowered for word in ("cơ bản", "bao nhiêu tháng", "điều nào", "quy định tại điều"))
        facts = []
        for (_, article_id), provisions in grouped.items():
            if article_numbers and not any(re.search(rf"\b{re.escape(number)}\b", article_id) for number in article_numbers):
                continue
            if wants_maximum:
                incarceration = [row for row in provisions if re.search(
                    r"\b(tù|tử hình)\b",
                    f"{row.get('penalty') or ''} {row.get('provision_text') or row.get('text') or ''}",
                    re.IGNORECASE,
                )]
                selected = [max(incarceration or provisions, key=lambda row: row["provision_number"])]
            elif wants_basic:
                selected = [min(provisions, key=lambda row: row["provision_number"])]
            else:
                selected = [min(provisions, key=lambda row: row["provision_number"])]
            for row in selected:
                label = "Clause" if hint else "khoản"
                number = row["provision_number"]
                detail = row.get("penalty") or row.get("provision_text") or row.get("text") or ""
                article_title = row.get("article_title") or row.get("title") or ""
                doc_id = row.get("provision_doc_id") or row.get("doc_id") or ""
                fact = f"[{row['article_id']} - {article_title}] {label} {number}: "
                if wants_maximum:
                    detail = re.sub(r"^phạt\s+", "", detail, flags=re.IGNORECASE)
                    fact += f"Mức phạt tù tối đa theo khoản này: {detail}"
                else:
                    fact += detail
                if row.get("crime"):
                    fact = f"Tội danh {row['crime']} — {fact}"
                facts.append(f"{fact} (doc_id={doc_id})")
                if len(facts) >= limit:
                    return facts
        return facts

    def _explicit_person_ids(self, question: str) -> list[str]:
        """Resolve named people/aliases first so unrelated retrieved chunks do not widen case scope."""
        rows = self.run(
            """
            MATCH (p:Person)
            WHERE (p.name IS NOT NULL AND size(p.name) >= 3 AND toLower($q) CONTAINS toLower(p.name))
               OR any(alias IN coalesce(p.aliases, [])
                      WHERE size(alias) >= 3 AND toLower($q) CONTAINS toLower(alias))
            RETURN elementId(p) AS id
            """,
            q=question,
        )
        return list(dict.fromkeys(row["id"] for row in rows if row.get("id")))

    def _custom_context(self, question: str, doc_ids: list[str], max_facts: int) -> list[str]:
        person_ids = self._explicit_person_ids(question)
        seed_question = _specific_drug_seed(question)
        seed_ids, seed_facts = self.seed_facts(
            seed_question, [] if person_ids else doc_ids,
            skip_labels=("Article", "Provision", "PenaltyCondition", "Definition"),
            limit=max_facts,
        )
        retrieval_ids = person_ids or seed_ids
        facts = list(seed_facts)
        q_lower = question.lower()
        if any(term in q_lower for term in ("là gì", "định nghĩa", "nghĩa là gì", "được hiểu")):
            definitions = self.run(
                """
                MATCH (a:Article)-[:HAS_DEFINITION]->(d:Definition)
                WHERE toLower($q) CONTAINS toLower(d.term)
                RETURN a.id AS article_id, a.title AS title, d.term AS term, d.text AS text, d.doc_id AS doc_id
                ORDER BY size(d.term) DESC LIMIT $limit
                """,
                q=question, limit=max_facts,
            )
            facts.extend(f"[{row['article_id']} - {row['title']}] {row['term']}: {row['text']} (doc_id={row['doc_id']})"
                         for row in definitions)

        case_rows = self.run(
            """
            MATCH (k:Case)
            WHERE elementId(k) IN $ids OR EXISTS { MATCH (seed)--(k) WHERE elementId(seed) IN $ids }
            OPTIONAL MATCH (person:Person)-[participation:PARTICIPATED_IN]->(k)
            WHERE elementId(person) IN $ids
            WITH k, [charge IN collect(DISTINCT participation.charge)
                     WHERE charge IS NOT NULL AND charge <> ''] AS person_charges
            OPTIONAL MATCH (k)-[:CHARGED_WITH]->(c:Crime)<-[:DEFINES]-(a:Article)-[:HAS_PROVISION]->(v:Provision)
            WHERE size(person_charges) = 0 OR c.name IN person_charges
            RETURN k.id AS case_id, k.name AS case_name, k.summary AS summary, k.stage AS stage, k.doc_id AS doc_id,
                   c.name AS crime, a.id AS article_id, a.title AS article_title,
                   v.number AS provision_number, v.penalty AS penalty, v.text AS provision_text,
                   v.doc_id AS provision_doc_id
            ORDER BY k.name, a.id, v.number LIMIT $limit
            """,
            ids=retrieval_ids, limit=max(1, max_facts * 6),
        )
        quantity_question = bool(re.search(r"\b(gram|gam|kg|kilôgam|khối lượng|trọng lượng|khoản nào)\b", q_lower))
        seen_cases = set()
        for row in case_rows:
            case_id = row.get("case_id")
            if case_id and case_id not in seen_cases:
                seen_cases.add(case_id)
                summary = row.get("summary") or ""
                stage = f"; giai đoạn: {row['stage']}" if row.get("stage") else ""
                facts.append(f"Vụ việc '{row['case_name']}': {summary}{stage} (doc_id={row['doc_id']})")
            if row.get("crime"):
                facts.append(f"Tội danh liên kết: {row['crime']} (doc_id={row.get('provision_doc_id') or row.get('doc_id')})")
        if not quantity_question:
            facts.extend(self._legal_facts(case_rows, question, max_facts - len(facts)))

        if quantity_question:
            matching_conditions = self.run(
                """
                MATCH (k:Case)-[inv:INVOLVES]->(s:Substance)
                WHERE (elementId(k) IN $ids OR EXISTS { MATCH (seed)--(k) WHERE elementId(seed) IN $ids })
                  AND toLower($q) CONTAINS toLower(s.name)
                MATCH (k)-[:CHARGED_WITH]->(c:Crime)<-[:DEFINES]-(a:Article)-[:HAS_PROVISION]->(v:Provision)
                      -[:HAS_CONDITION]->(t:PenaltyCondition)-[:APPLIES_TO]->(s)
                WHERE inv.amount_value IS NOT NULL AND inv.unit = t.unit AND t.min_amount IS NOT NULL
                  AND inv.amount_value >= t.min_amount
                  AND (t.max_amount IS NULL OR (t.max_exclusive AND inv.amount_value < t.max_amount)
                       OR (NOT t.max_exclusive AND inv.amount_value <= t.max_amount))
                RETURN DISTINCT k.name AS case_name, s.name AS substance, inv.raw_amount AS amount,
                       a.id AS article_id, a.title AS article_title, v.number AS provision_number,
                       v.penalty AS penalty, t.point AS point, t.text AS condition_text, t.doc_id AS doc_id
                ORDER BY v.number DESC LIMIT $limit
                """,
                ids=retrieval_ids, q=question, limit=max_facts,
            )
            for row in matching_conditions:
                facts.append(f"Vụ '{row['case_name']}' có {row['amount']} {row['substance']}; "
                             f"[{row['article_id']}] khoản {row['provision_number']} điểm {row['point']}: "
                             f"{row['condition_text']} — {row['penalty']} (doc_id={row['doc_id']})")

        article_numbers = re.findall(r"[đd]iều\s+(\d+)", question, re.IGNORECASE)
        if article_numbers:
            direct_rows = self.run(
                """
                MATCH (a:Article)-[:HAS_PROVISION]->(v:Provision)
                WHERE any(number IN $numbers WHERE a.id CONTAINS number)
                RETURN a.id AS article_id, a.title AS article_title, v.number AS provision_number,
                       v.penalty AS penalty, v.text AS provision_text, v.doc_id AS provision_doc_id
                ORDER BY a.id, v.number LIMIT $limit
                """,
                numbers=article_numbers, limit=max_facts,
            )
            facts.extend(self._legal_facts(direct_rows, question, max_facts - len(facts)))
        return list(dict.fromkeys(facts))[:max_facts]

    def _hint_context(self, question: str, doc_ids: list[str], max_facts: int) -> list[str]:
        person_ids = self._explicit_person_ids(question)
        seed_question = _specific_drug_seed(question)
        seed_ids, facts = self.seed_facts(
            seed_question, [] if person_ids else doc_ids, skip_labels=("Article", "Clause"), limit=max_facts,
        )
        retrieval_ids = person_ids or seed_ids
        rows = self.run(
            """
            MATCH (k:Case)
            WHERE elementId(k) IN $ids OR EXISTS { MATCH (seed)--(k) WHERE elementId(seed) IN $ids }
            OPTIONAL MATCH (person:Person)-[participation:INVOLVED_IN]->(k)
            WHERE elementId(person) IN $ids
            WITH k, [charge IN collect(DISTINCT participation.charge)
                     WHERE charge IS NOT NULL AND charge <> ''] AS person_charges
            OPTIONAL MATCH (k)-[:CHARGED_WITH]->(c:Crime)<-[:DEFINES]-(a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE size(person_charges) = 0 OR c.name IN person_charges
            RETURN k.name AS case_name, k.summary AS summary, k.doc_id AS doc_id, c.name AS crime,
                   a.id AS article_id, a.title AS article_title, cl.number AS provision_number,
                   cl.penalty AS penalty, cl.text AS provision_text, cl.doc_id AS provision_doc_id
            ORDER BY k.name, a.id, cl.number LIMIT $limit
            """,
            ids=retrieval_ids, limit=max(1, max_facts * 6),
        )
        for row in rows:
            if row.get("case_name"):
                facts.append(f"Vụ việc '{row['case_name']}': {row.get('summary') or ''} (doc_id={row.get('doc_id')})")
            if row.get("crime"):
                facts.append(f"Tội danh liên kết: {row['crime']}")
        facts.extend(self._legal_facts(rows, question, max_facts - len(facts), hint=True))
        article_numbers = re.findall(r"[đd]iều\s+(\d+)", question, re.IGNORECASE)
        if article_numbers:
            direct_rows = self.run(
                """
                MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)
                WHERE any(number IN $numbers WHERE a.id CONTAINS number)
                RETURN a.id AS article_id, a.title AS article_title, cl.number AS provision_number,
                       cl.penalty AS penalty, cl.text AS provision_text, cl.doc_id AS provision_doc_id
                ORDER BY a.id, cl.number LIMIT $limit
                """,
                numbers=article_numbers, limit=max_facts,
            )
            facts.extend(self._legal_facts(direct_rows, question, max_facts - len(facts), hint=True))
        return list(dict.fromkeys(facts))[:max_facts]

# ---------------------------------------------------------------------------------------------- KG-2

def build_graph(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document],
                llm_fn: Callable[..., str]) -> None:
    """Load both KBs into an empty graph. llm_fn(prompt, json_mode=False) -> str (metered OpenAI chat)."""
    ontology = os.getenv("DRUG_KG_ONTOLOGY", "custom").strip().lower()
    if ontology == "hint":
        graph.suggested_constraints()
        articles = [parse_law_article(doc) for doc in law_docs]
        known_crimes = [article["crime"] for article in articles if article["crime"]]
        for article in articles:
            graph.add_law_article(article)
        for doc in news_docs:
            for case in extract_news_cases(doc, llm_fn, known_crimes):
                graph.add_news_case(case, doc)
        return
    if ontology != "custom":
        raise ValueError(f"Unknown DRUG_KG_ONTOLOGY={ontology!r}; expected 'custom' or 'hint'.")

    graph.custom_constraints()
    articles = [parse_custom_law_article(doc) for doc in law_docs]
    definitions = [definition for doc in law_docs for definition in parse_statutory_definitions(doc)]
    graph.add_custom_law(articles, definitions)
    known_crimes = [article["crime"] for article in articles if article["crime"]]
    cases = []
    for doc in news_docs:
        for index, case in enumerate(extract_custom_news_cases(doc, llm_fn, known_crimes), start=1):
            case["id"] = f"{doc.id}::case-{index}"
            case["location_id"] = _person_key(case["location"])
            cases.append(case)
    graph.add_custom_news_cases(cases)

# ---------------------------------------------------------------------------------------------- KG-4

GRAPH_PROMPT = """Trả lời câu hỏi chỉ dựa trên ngữ cảnh (đoạn văn bản và dữ kiện từ knowledge graph).
Nêu rõ số Điều luật khi có. Nếu ngữ cảnh không đủ, nói không đủ thông tin.

Nguyên tắc sử dụng dữ kiện:
- Không suy đoán số Điều từ tên tội hoặc mức phạt; nếu graph nêu Điều luật thì giữ đúng số Điều và khoản đó.
- Giữ đúng giai đoạn tố tụng được ghi trong nguồn (bị bắt, bị truy tố, xét xử, kết án).
- Nếu cạnh PARTICIPATED_IN của người được hỏi có `charge`, ưu tiên cáo buộc đó thay cho hành vi khác trong tóm tắt vụ hoặc đoạn văn bản.
- Khi hỏi mức tù tối đa, dùng khoản có mức tù cao nhất cho tội danh đã khớp; không xem khoản phạt tiền bổ sung là mức tù tối đa.
- Nếu graph fact có nhãn “Mức phạt tù tối đa”, chép nguyên văn khung phạt từ fact đó; không lấy con số khác trong chunk.
- Với câu hỏi tổng hợp, đọc toàn bộ dữ kiện graph và nêu đủ sự kiện khác nhau; không dừng danh sách ở ba vụ. Gộp các bản tin chỉ khi người và diễn biến cho thấy đó là cùng sự kiện.

Đoạn văn bản:
{chunks}

Dữ kiện graph:
{facts}

Khi chunk mâu thuẫn, dùng graph facts cho đường nối người–vụ–tội–Điều.

Câu hỏi: {question}
Trả lời:"""

class GraphRAGAgent:
    """Hybrid GraphRAG: the same vector top-k as flat RAG, plus facts expanded from the graph."""

    def __init__(self, store: EmbeddingStore, graph: Neo4jGraph, llm_fn: Callable[[str], str]) -> None:
        self.store = store
        self.graph = graph
        self.llm_fn = llm_fn

    def answer(self, question: str, top_k: int = 3) -> str:
        hits = self.store.search(question, top_k=top_k)
        doc_ids = list(dict.fromkeys(
            hit.get("metadata", {}).get("doc_id") for hit in hits
            if hit.get("metadata", {}).get("doc_id")
        ))
        graph_facts = self.graph.context(question, doc_ids)
        chunks = "\n\n".join(f"[{index}] {hit.get('content', '')}" for index, hit in enumerate(hits, start=1))
        facts = "\n".join(f"- {fact}" for fact in graph_facts) or "(Không có dữ kiện graph phù hợp.)"
        prompt = GRAPH_PROMPT.format(facts=facts, chunks=chunks or "(Không tìm thấy đoạn văn bản phù hợp.)",
                                     question=question)
        return self.llm_fn(prompt)
