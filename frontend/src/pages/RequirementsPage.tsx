export function RequirementsPage() {
  return <div className="requirements-workspace">
    <header className="page-heading">
      <div className="eyebrow">МОДУЛЬ 03 / СОПОСТАВЛЕНИЕ</div>
      <h1>ТУ и ЗнП<span className="heading-dot">.</span></h1>
      <p className="muted">Проверка соответствия задания на проектирование техническим условиям
        с опорой на исходные фрагменты и решение инженера.</p>
    </header>
    <section className="panel requirements-intro">
      <span className="status-note">В разработке</span>
      <h2>Сопоставление требований</h2>
      <p>Этот раздел будет извлекать отдельные требования ТУ, искать соответствующие
        положения ЗнП и формировать замечания с точными цитатами.</p>
      <div className="requirements-steps">
        <div><span>01</span><strong>Извлечение</strong><p>Выделение атомарных требований и параметров.</p></div>
        <div><span>02</span><strong>Поиск</strong><p>Текстовые и семантические кандидаты в ЗнП.</p></div>
        <div><span>03</span><strong>Проверка</strong><p>Числа, ограничения и решение эксперта.</p></div>
      </div>
      <p className="muted requirements-disclaimer">Автоматический анализ пока не реализован.
        POST /api/v1/requirements/analyze возвращает HTTP 501. Результаты не имитируются.</p>
    </section>
  </div>
}
