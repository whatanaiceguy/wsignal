# What a weak signal is

Derived from 100 labelled rows, read end to end. The full version with quotations is `documentation/METHODOLOGY.md`. Quotes remain in Russian: they are evidence, and translated text is not a quote.

**The rows illustrate shape, not an answer key.** They show a properly named threshold, denominator and how small “small” must be. A technology quoted below is neither taken, settled nor disqualified: judge it only from this run's retrieved evidence; reasoning from the rows is memory, and nothing here rests on it.

## The unit: a technology plus a transition

Judge **a claim that a named technology is crossing a named threshold now**, not a technology alone. One technology may support several claims of different strength: neuromorphic chips appear three times—as an AI-workload architecture, a component in standard EDA flows, and always-on industrial sensing—with three scores. Row #45: «Сигнал не в самих нейроморфных чипах, а в их появлении в библиотеках автоматизированного проектирования плат».

## Three axes

**Substance:** independent actors entering and committing resources. This is a count, not a judgement: how many entered within the window and whether the rate is rising. Count actors **across sources**, not within one: a survey naming Amazon, Google and Microsoft in one sentence is one source, not three actors.

**Faintness:** how far category recognition lags activity, measured where recognition is recorded: procurement lines, analyst taxonomies, benchmarks, standards and the field's mainstream narrative. It is a degree and ratio: more independent activity relative to recognition means fainter. An analyst mention, trade-press article or vendor page starts recognition and lowers faintness a little; it does not end it. Faintness ends only when the category is mainstream in its field: a standing analyst category, procurement at volume, and most of the field involved and spending at its own scale. A newly noticed topic—not yet billions with half the field involved—remains a weak signal and can be stronger than an untouched one.

**Delivery gap:** committed activity outruns delivered activity, a separate measure of early versus mature regardless of loudness: «Сделок много, но ни одного работающего SMR у ЦОД» (#34), «продакшена нет» (#71), «почти все работы по RL-управлению остаются в симуляции» (#81). Contracts without deployments are early; deployments without contracts are not a signal.

## Every measure is graded

No yes/no judgements. Substance, momentum, faintness and every pattern's strength are 0 to 1, with evidence. A pattern is unasserted or has a strength; “not confirmed” is low strength with a reason, not an end to the claim. Code computes the final score; set each number from retrieved evidence and explain it in prose.

- **substance:** real independent committed activity—actors, money, products, deployments, artifacts—against the field's scale. 0 = nothing real; 1 = unmistakably real and large.
- **momentum:** growth within the window—new actors, rounds, releases, dated. 0 = flat or shrinking; 1 = accelerating.
- **faintness:** how far recognition lags activity. 0 = mainstream in its field; 1 = heavy activity with no recognition.

A loud, mature, real technology is a strong signal (high substance, low faintness), not noise. Noise is low substance: hype, a dead topic or nothing real behind a claim. Illustrations of scale, never evidence: solid-state drives, a leading foundry's 7nm FinFET in volume production, and tandem OLED panels in shipping devices have substance near 1 and faintness near 0; press releases, demos and token sales with nothing shipping have substance near 0, however loud. A weak signal is real, independent, growing activity whose category recognition lags.

## Scale is relative to the field

A sum means nothing without its field: half a billion dollars is small in AI infrastructure and enormous in mining sensors. Judge each amount, count and deployment against field size; state the field and scale. Many independent actors committing small amounts is a strong weak-signal shape: independent activity is real and still small. Large money is not loudness; large money with little recognition is `money_over_attention`.

## The technology as named

Assess technology and transition **at their named scope**; do not narrow a claim to ease refutation or avoid a strong/noise verdict, or widen it. An incidental neighbour is a lead, not a rename. If retrieved evidence shows the named topic is not moving but a better-named transition is, call `rename_topic` with reason and verdict on the name left. Judge it there; do not research it after the split. A strong or noise verdict is a result, not grounds for silent renaming.

## Faintness types: “few mentions” is not one

A claim names its asserted type:

- **`no_procurement_category`** — no procurement or analyst category. «категории "agent IAM" в закупках ещё нет» (#2); «не имеет ни аналитической категории Gartner, ни бенчмарков» (#20). Weakened by an analyst category page, benchmark or procurement line item; each starts recognition and lowers faintness. Gone when the category is standing and procured at volume across the field.
- **`venue_without_recognition`** — discussion only where it confers no recognition. «обсуждается пока инженерными блогами и вендорскими whitepaper, а не аналитиками рынка» (#10); «Освещение почти полностью замкнуто на крипто-медиа» (#58). Weakened by tier-1 trade press coverage of the field itself, according to its continuity and centrality. One or two articles start recognition, not end faintness.
- **`mainstream_is_the_neighbour`** — the same field's mainstream narrative concerns a neighbour; the strongest and hardest to fake. «Мейнстрим-нарратив по-прежнему "GPU + HBM"» (#8); «Внимание рынка приковано к гуманоидам, а не к квадропедам» (#12). Measure named-topic / neighbour volume with one instrument and window; a narrowing ratio weakens it proportionally.
- **`misfiled_domain`** — filed under another field. «Тема воспринимается как "космос", а не как ветвь edge-инфраструктуры» (#18).
- **`bottleneck_not_market`** — described as a bottleneck, not a market; commercially interesting because a constraint becomes a business line. «"Грязная, неблагодарная" работа по сбору данных описывается как узкое место, а не как рынок» (#14); «Инвестиционные обзоры пишут про модели и железо, а не про сертификацию» (#13).

## Substance types: money is a ratio, not a sum

- **`money_over_attention`** — disproportion, not sum. «привлёк лишь ~$40M — на два порядка меньше общего финансирования агентной безопасности» (#4); «Раунды на уровне $5 млн — почти невидимы в потоке новостей» (#33).
- **`funding_trajectory`** — step change across rounds. «$7M seed (2024) → $50M Series A (2025) → $150M при оценке $1.25B (февраль 2026)» (#49).
- **`strategic_money`** — corporate/industry rather than venture money. «инвесторы — корпоративные и климатические фонды: BHP Ventures, Rio Tinto» (#28).
- **`incumbent_entry`** — large players entering signal consolidation. «крупные CDN/сетевые вендоры уже выпустили продукты» (#20). Company size is not category maturity: 45 of 100 positive rows name a large player, and the score-7 band is densest in them. The category, not companies, must be unrecognised.
- **`strategic_acquisition`** — acquisition by a strategic buyer. «M&A со стороны AMD» (#33).
- **`stealth_exits`** — several stealth exits in a short window. «за 12 месяцев — минимум 3 выхода из stealth» (#2).
- **`first_independent_artifact`** — a disinterested party measures, audits or compares it. «первые независимые аудиты MCP-серверов (33 сервера) опубликованы только в апреле 2026» (#4).
- **`regulatory_trigger`** — regulator or standard makes a topic a requirement. «EU AI Act Transparency Code конвертирует исследовательскую тему в продуктовое требование» (#25).
- **`supply_demand_asymmetry`** — attacks exist, defences do not; demand is codified, product is not. «Атаки описаны, а защит почти нет» (#76).

## Reversal

A category of its own: a topic considered dead returns. «Категория считалась "мёртвой" после сворачивания Mythic в 2022; возврат к финансированию — классический слабый сигнал разворота тренда» (#44). A present-day crawl cannot detect reversal; evidence is the earlier death.

## Smallness needs a denominator

Do not write “few”; say how few and against what: «совокупная пропускная способность 12 сетей — ~2,75% от дневного объёма токенов OpenAI» (#58); «лишь ~14% DEX начали пилоты ZKP-KYC в 2025» (#68). **“0 mentions across 12 venues, 40 queries” is an argument; “Hardly discussed” asserts nothing and is worth nothing.** A denominator strengthens a measure, but early technologies often lack procurement and deployment totals. State its absence and grade the measure on what was found; missing denominator alone never makes a claim insufficient.

## Hard rules

1. **Nothing rests on model knowledge without confirmed retrieval from open sources.** If it is not in a retrieved document, it does not exist.
2. **Reproduce quotes verbatim** from text returned by `fetch` or corpus `read`, in the source's language. They are checked against the stored page; mismatches appear in output.
3. **Analyst reports are a denominator, not a source of candidates.** Gartner/IDC/Forrester appearance shows recognition has started and lowers faintness according to centrality and whether it is standing. A mention or retelling is not a standing category.
4. **Never rank by cumulative counters.** All-time downloads, stars and citations rank by age; use windowed or dated quantities only.
5. **First year of mention** is meaningful only from sources with real historical depth; name the source, not a bare number.
6. **Match query language to the index.** An English-only index answers Russian queries with unrelated noise, not clear emptiness; emptiness is half the argument, so distinguish them. Academic indexes are English; the open web and our store hold both. Ask a term in its existing language. `list_sources` describes each index.
7. **No source is out of bounds or privileged.** Ask anything that could hold the answer. Source strengths are properties recorded in `list_sources`, not access rules.
8. **Report the harness, then carry on.** Whenever a tool blocks you, contradicts its own description, wastes your effort, or you need something no tool gives, leave a `harness_note` and continue the task. Notes change nothing in the run.
