"""Unit tests for deterministic reconciliation of atomic clauses from fragments."""
from app.core.models import Fragment
from app.requirements.extraction import (
    MAX_CLAUSE_CHARS,
    extract_clauses,
    is_service_clause,
)


def make_fragment(
    page: int,
    position: int,
    text: str,
    *,
    block_type: str = "paragraph",
    section_path: str | None = None,
    fragment_id: str | None = None,
) -> Fragment:
    return Fragment(
        id=fragment_id or f"f-{page}-{position}",
        document_id="doc-1",
        page=page,
        position=position,
        text=text,
        block_type=block_type,
        section_path=section_path,
    )


def test_numbering_variants_start_new_clauses():
    clauses = extract_clauses([
        make_fragment(1, 0, "1.2.3. Требование по резервированию"),
        make_fragment(1, 1, "п. 4.1 Максимальная мощность 150 кВт"),
        make_fragment(1, 2, "4.2) Класс напряжения 10 кВ"),
        make_fragment(1, 3, "Раздел 3. Общие положения"),
        make_fragment(1, 4, "Текст внутри раздела"),
    ])

    assert [clause.clause_number for clause in clauses] == [
        "1.2.3", "4.1", "4.2", "Раздел 3",
    ]
    assert clauses[3].text.endswith("Текст внутри раздела")


def test_clause_continues_across_pages():
    first = make_fragment(1, 0, "1.1. Максимальная мощность 150 кВт")
    continuation = make_fragment(2, 0, "Дополнительное условие по мощности.")
    second = make_fragment(2, 1, "1.2. Точка присоединения")

    clauses = extract_clauses([first, continuation, second])

    assert len(clauses) == 2
    assert clauses[0].page == 1
    assert clauses[0].fragment_id == first.id
    assert clauses[0].fragment_ids == [first.id, continuation.id]
    assert "Дополнительное условие" in clauses[0].text
    assert clauses[1].page == 2


def test_duplicate_clauses_are_deduplicated():
    clauses = extract_clauses([
        make_fragment(1, 0, "1.1. Установить прибор учета"),
        make_fragment(1, 1, "2.4. Установить прибор учета"),
    ])

    assert len(clauses) == 1
    assert clauses[0].clause_number == "1.1"


def test_noise_fragments_are_skipped():
    clauses = extract_clauses([
        make_fragment(1, 0, "12"),
        make_fragment(1, 1, "— — —"),
        make_fragment(1, 2, "М.П."),
        make_fragment(1, 3, "Подпись"),
        make_fragment(1, 4, "аб"),
        make_fragment(1, 5, "1.1. Настоящее требование"),
    ])

    assert len(clauses) == 1
    assert clauses[0].clause_number == "1.1"


def test_long_clause_is_split_by_parts():
    long_text = "1.1. " + "А" * 1500 + "\n" + "Б" * 1500 + "\n" + "В" * 1500
    clauses = extract_clauses([make_fragment(1, 0, long_text)])

    assert len(clauses) == 2
    assert clauses[0].clause_number == "1.1 (ч. 1/2)"
    assert clauses[1].clause_number == "1.1 (ч. 2/2)"
    assert all(len(clause.text) <= MAX_CLAUSE_CHARS for clause in clauses)


def test_duplicate_numbers_get_unique_suffix():
    clauses = extract_clauses([
        make_fragment(1, 0, "1.1. Первое требование по мощности"),
        make_fragment(1, 1, "1.1. Второе требование по учету"),
    ])

    assert [clause.clause_number for clause in clauses] == ["1.1", "1.1#2"]


def test_short_preamble_is_dropped_and_long_preamble_kept():
    short = extract_clauses([
        make_fragment(1, 0, "О документе"),
        make_fragment(1, 1, "1.1. Пункт требований"),
    ])
    assert [clause.clause_number for clause in short] == ["1.1"]

    long = extract_clauses([
        make_fragment(1, 0, "Настоящие технические условия распространяются на проектирование."),
        make_fragment(1, 1, "1.1. Пункт требований"),
    ])
    assert [clause.clause_number for clause in long] == ["Преамбула", "1.1"]
    assert long[0].page == 1


def test_title_page_is_not_a_requirement():
    clauses = extract_clauses([
        make_fragment(1, 0, "АО «Сетевая компания»"),
        make_fragment(1, 1, "ТЕХНИЧЕСКИЕ УСЛОВИЯ"),
        make_fragment(1, 2, "Номер ТУ: ТУ-2026/ГУ-236-УрГРЭС"),
        make_fragment(1, 3, "Дата выдачи: «___» __________ 2026 г."),
        make_fragment(1, 4, "1.1. Максимальная мощность 150 кВт."),
    ])

    assert [clause.clause_number for clause in clauses] == ["1.1"]


def test_heading_without_body_does_not_create_requirement():
    clauses = extract_clauses([
        make_fragment(
            1, 0, "1. Общие положения", block_type="heading",
            section_path="1. Общие положения",
        ),
        make_fragment(
            1, 1, "2.1. Схема выдачи мощности", block_type="heading",
            section_path="2.1. Схема выдачи мощности",
        ),
        make_fragment(
            1, 2, "Раздел 3", block_type="heading", section_path="Раздел 3",
        ),
    ])

    assert clauses == []


def test_heading_gives_number_and_context_to_unlabelled_body():
    heading = make_fragment(
        1, 0, "2. Характеристика генерирующей установки", block_type="heading",
        section_path="2. Характеристика генерирующей установки",
    )
    body = make_fragment(1, 1, "Установленная электрическая мощность 236 МВт.")

    clauses = extract_clauses([heading, body])

    assert len(clauses) == 1
    assert clauses[0].clause_number == "2"
    assert clauses[0].text == body.text
    assert clauses[0].section_path == heading.section_path
    assert clauses[0].fragment_id == body.id

    section = extract_clauses([
        make_fragment(
            1, 0, "Раздел 2. Схема выдачи мощности", block_type="heading",
            section_path="Раздел 2. Схема выдачи мощности",
        ),
        body,
    ])
    assert [clause.clause_number for clause in section] == ["Раздел 2"]


def test_heading_between_points_does_not_glue_next_text_to_previous():
    first = make_fragment(1, 0, "1.1. Первое требование по мощности.")
    heading = make_fragment(
        1, 1, "2. Схема выдачи мощности", block_type="heading",
        section_path="2. Схема выдачи мощности",
    )
    unlabelled = make_fragment(1, 2, "Выдача мощности предусматривается на напряжении 110 кВ.")
    second = make_fragment(1, 3, "2.1. Второе требование по учету.")

    clauses = extract_clauses([first, heading, unlabelled, second])

    assert [clause.clause_number for clause in clauses] == ["1.1", "2", "2.1"]
    assert clauses[0].text == first.text
    assert clauses[1].text == unlabelled.text
    assert clauses[2].text == second.text


def test_toc_and_service_headings_are_not_clauses():
    clauses = extract_clauses([
        make_fragment(1, 0, "Содержание", block_type="heading",
                      section_path="Содержание"),
        make_fragment(1, 1, "1. Общие положения ........ 3"),
        make_fragment(1, 2, "2. Схема выдачи мощности ……… 5"),
        make_fragment(1, 3, "Приложение А. Перечень сокращений",
                      block_type="heading", section_path="Приложение А. Перечень сокращений"),
        make_fragment(1, 4, "Сокращение | Расшифровка", block_type="table_row",
                      section_path="Приложение А. Перечень сокращений"),
        make_fragment(1, 5, "Термины и определения", block_type="heading",
                      section_path="Термины и определения"),
        make_fragment(1, 6, "Утверждаю", block_type="heading",
                      section_path="Утверждаю"),
        make_fragment(1, 7, "Технические условия", block_type="heading",
                      section_path="Технические условия"),
        make_fragment(1, 8, "1. Общие положения", block_type="heading",
                      section_path="1. Общие положения"),
    ])

    assert clauses == []


def test_service_clauses_are_recognised_but_not_removed_by_extraction():
    assert is_service_clause("Основание: заявка на технологическое присоединение")
    assert is_service_clause("Срок действия технических условий — 2 года")
    assert is_service_clause("Приложение 1. Схема подключения")
    assert not is_service_clause("1.1. Максимальная мощность 150 кВт")

    clauses = extract_clauses([
        make_fragment(1, 0, "Основание: заявка на технологическое присоединение № 123."),
    ])
    assert len(clauses) == 1  # filtering is the caller's decision (router)
