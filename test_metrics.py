import re

# ==============================================================================
# 1. ЭТАЛОННАЯ РАЗМЕТКА (GROUND TRUTH)
# Список всех уникальных нормативов, которые РЕАЛЬНО упоминаются в тексте ГОСТ 32144-2013
# ==============================================================================
GROUND_TRUTH = {
    "ГОСТ 1.0-92",
    "ГОСТ 1.2-2009",
    "ГОСТ 29322-92",
    "ГОСТ 30804.4.30-2013",
    "ГОСТ 30804.4.7-2013",
    "ГОСТ 30804.3.3-2013",
    "ГОСТ Р 51317.4.15-2012",
    "ГОСТ Р 54149-2010",
    "EN 50160:2010",
    "IEC 61000-4-30:2008",
    "IEC 61000-4-7:2009",
    "IEC 61000-3-3:2008",
    "IEC 61000-4-15:2010",
    "IEC 61000-2-8:2002"
}

# ==============================================================================
# 2. РЕЗУЛЬТАТЫ ИЗВЛЕЧЕНИЯ СИСТЕМОЙ
# Вставь сюда реальный список, который вернул твой API (из Swagger или фронтенда).
# Ниже приведен пример того, что система вернула ранее (некоторые без годов).
# ==============================================================================
EXTRACTED_RESULTS = [
    "ГОСТ 1.0",
    "ГОСТ 1.2",
    "ГОСТ 29322",
    "ГОСТ 30804.4.30",
    "ГОСТ 30804.4.7",
    "ГОСТ 30804.3.3",
    "ГОСТ Р 51317.4.15",
    "ГОСТ Р 54149",
    "EN 50160:2010",
    "IEC 61000"
]

def normalize_for_comparison(code: str) -> str:
    """Упрощенная нормализация для сравнения (убирает лишние пробелы, верхний регистр)"""
    return re.sub(r'\s+', ' ', code).strip().upper().replace(" ", "")

def is_match(extracted: str, ground_truth_set: set) -> tuple[bool, str | None]:
    """
    Проверяет, соответствует ли извлеченный код какому-либо эталонному.
    Считаем совпадением только полное нормализованное обозначение.
    Совпадение по началу номера не доказывает совпадение редакции.
    """
    ext_norm = normalize_for_comparison(extracted)
    
    for gt in ground_truth_set:
        gt_norm = normalize_for_comparison(gt)
        # Проверка: полное совпадение ИЛИ извлеченное является началом эталонного (длина > 8 для избежания ложных срабатываний типа "ГОСТ")
        if ext_norm == gt_norm:
            return True, gt
            
    return False, None

def calculate_metrics(ground_truth: set, extracted: list):
    tp = 0  # True Positives (Верно извлеченные)
    fp = 0  # False Positives (Мусор / галлюцинации)
    
    matched_gt = set()

    print("--- Детальный разбор извлеченных ссылок ---")
    for ext in extracted:
        is_tp, matched_code = is_match(ext, ground_truth)
        if is_tp and matched_code not in matched_gt:
            tp += 1
            matched_gt.add(matched_code)
            print(f"✅ True Positive : '{ext}' -> совпал с эталоном '{matched_code}'")
        else:
            fp += 1
            print(f"❌ False Positive: '{ext}' -> не найден в эталоне (мусор или ошибка)")

    # False Negatives - это то, что есть в эталоне, но не было извлечено
    missed = ground_truth - matched_gt
    fn = len(missed)
    
    if missed:
        print("\n--- Пропущенные нормативы (False Negatives) ---")
        for m in sorted(missed):
            print(f"⚠️ Пропущено: '{m}'")

    # Расчет метрик
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0

    print("\n" + "="*50)
    print("📊 ИТОГОВЫЕ МЕТРИКИ КАЧЕСТВА ИЗВЛЕЧЕНИЯ")
    print("="*50)
    print(f"Всего в эталоне (Actual):             {len(ground_truth)}")
    print(f"Всего извлечено системой (Predicted): {len(extracted)}")
    print(f"True Positives (TP):                  {tp}")
    print(f"False Positives (FP):                 {fp}")
    print(f"False Negatives (FN):                 {fn}")
    print("-" * 50)
    print(f"🎯 Precision (Точность):              {precision:.2%}")
    print(f"   (Какая доля извлеченных ссылок является верной)")
    print(f"🔍 Recall (Полнота):                  {recall:.2%}")
    print(f"   (Какая доля реальных ссылок была найдена)")
    print("="*50)
    print("Частичные обозначения без года не считаются точным совпадением.")
    print("EXTRACTED_RESULTS заданы вручную: это пример, не замер API.")

if __name__ == "__main__":
    calculate_metrics(GROUND_TRUTH, EXTRACTED_RESULTS)