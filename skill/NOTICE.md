# NOTICE

Скіл `yurko` — частина репозиторію Yurko MCP (ліцензія MIT, див. `../LICENSE`). Окремі частини текстів адаптовано з творів під Apache License 2.0; копія ліцензії — `LICENSE-APACHE-2.0` у цій теці. Усі адаптовані тексти переписано українською, скорочено й змінено під ведення справи на першоджерелах через МСП Yurko; схвалення з боку авторів не заявляється.

## Адаптовані твори (Apache License 2.0)

**claude-for-legal** — https://github.com/anthropics/claude-for-legal, коміт `4a6c651889c97cc9140580363c73e0eb17379c2b`.
Використано: `litigation-legal/CLAUDE.md` (розділ «Shared guardrails»: три відповіді замість мовчазного доповнення, тригер актуальності, перевірка фактів, названих людиною, теги походження, дослівні цитати, пінпойнт на всю тезу, «Retrieved-content trust», «Handling retrieved results», «Large input») і `litigation-legal/skills/brief-section-drafter/SKILL.md` (письмово чи усно, чесність щодо слабких доводів, повнота перевірки цитат «N з M», відлуння, не повтор).
Де: `references/sources.md`, `references/output.md`, `roles/verifier.md`.
Зміни: переклад, заміна словника тегів на джерела Yurko, вилучення процедури США й обліку справ in-house.

**adversarial-legal-review-pl** — https://github.com/matematicsolutions/awesome-matematic-skills-pl, коміт `1f7beb02aafee5e2951ff69b3f400fe5968f5d25`. Copyright (c) 2026 Wieslaw Mazur / MateMatic Solutions. Згідно з NOTICE цього репозиторію скіл має ліцензію Apache-2.0 і черпає патерни з `anttihero/lavern` (Apache-2.0) та `gregmos/memoforge`.
Використано: каркас змагального циклу з окремими ролями, «невизначено» як повноцінний висновок із причиною, обмежена кількість кіл, «завжди віддати результат людині».
Де: `references/court.md`, `roles/advocate-general.md`.
Зміни: ролі перебудовано за преюдиціальною процедурою Суду ЄС (учасники, референт колегії, суддя-доповідач, генеральний адвокат, судді колегії) і сценаріями; числову функцію вердикту не перенесено — висновки категоріями.

**Sustainable Opposing Counsel Review** — https://github.com/lawve-ai/awesome-legal-skills, `skills/sustainable-opposing-counsel-review-seth-chandler`, коміт `180fd1582cc56fb0fc122a8e734ff3a1a666b87a`. Copyright 2026 Seth J. Chandler (https://legaled.ai). Цей твір сам є похідним від «Opposing Counsel Review» Larissa Meredith-Flister (Apache-2.0).
Використано: два проходи атаки, тест відповіді одним реченням, тест довіри, стеля в п'ять доводів, «що не оспорюється», правила не атакувати визнані факти й не переносити правила чужого форуму.
Де: `roles/participant.md`.
Зміни: роль перетворено на учасника провадження з інституційним інтересом; результат — письмові зауваження з опорами.

**Judicial First Impression** — https://github.com/lawve-ai/awesome-legal-skills, `skills/judicial-first-impression-larissa-meredith-flister`, коміт `180fd1582cc56fb0fc122a8e734ff3a1a666b87a`. Автор — Larissa Meredith-Flister, ліцензія apache-2.0 (за frontmatter скіла).
Використано: холодне читання — про що справа, що вирішальне, що припущено без доведення, рівень переконаності і що змінило б висновок.
Де: `roles/rapporteur.md`, `roles/advocate-general.md`.
Зміни: переклад; вихід підлаштовано під підготовку слухання і висновок генерального адвоката.

## Натхнення без запозичення тексту

**memoforge** — https://github.com/gregmos/memoforge (MIT): загальна схема оркестрації (паралельні дослідники, заморожений пакет джерел, ізольовані рецензенти, чесний кінцевий стан). Текст не копіювався.
