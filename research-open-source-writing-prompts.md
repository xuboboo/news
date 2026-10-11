# 开源提示词与 SKILL.md 调研：新闻资讯写作与"去 AI 味"

> 以下片段均为实际抓取的原文（英文原样引用 + 中文要点）。star 数为 2026-10-11 抓取时数据；低 star 仓库已明确标注，其价值在方法而非热度。

## 1. 范式类：优秀 SKILL.md / 系统提示词的结构模式

**（1）frontmatter 极简，重内容放进被链接文件。** anthropics/skills[1]（抓取时 180,352 star）的官方模板 template/SKILL.md[2] 全文仅：

```
---
name: template-skill
description: Replace with description of the skill and when Claude should use it.
---

# Insert instructions below
```

官方 internal-comms/SKILL.md[3] 是渐进式披露范本，正文只做路由：原文 "1. Identify the communication type from the request  2. Load the appropriate guideline file from the examples/ directory  3. Follow the specific instructions in that file"。中文要点：主文件只写"判断 + 指路"，长范例另存按需加载。

**（2）description 是给路由器看的激活 API。** 社区文档 Paldom/node-skills 的 skill-authoring.md[5]（star 未验证）给出公式：

> **[What it does] + [When to use it — realistic trigger phrasings] + [What it is NOT for]**
> description rules: **one physical line** ... multi-line block scalars (|, >) have failed to load in some runtimes ... 150–400 chars is the sweet spot; front-load trigger keywords in the first ~120 chars.

中文要点：写清"做什么 + 何时触发 + 不用于什么"，把用户原话塞进前 120 字。

**（3）反模式要写成"合理化对照表"。** med-news-reporter[16] 先立铁律，再预判模型如何自我开脱：

> IRON LAW: No Unsourced Facts ... Anything missing is marked [待查證: specific description] in the draft, NOT silently filled in ... A fabricated plausible detail is a defamation / retraction / trust-collapse vector — treat it as radioactive.
> **Rationalization Table — these justifications DO NOT override the Iron Law:** | "This market size is widely known, I don't need a source" | Even public knowledge must trace to user-supplied material or be flagged. |

中文要点：只写"禁止编造"无用，必须把模型的狡辩理由逐条列出并驳回。

## 2. 指令类：真正能减少"AI 味"的具体 prompt 条款

1. **分层禁用词表**（humanize-ai-writing[8]）："delve, tapestry, testament, underscore, pivotal, intricate, meticulous, garner, vibrant, bolster, enduring, interplay, leverage (verb), elevate, unlock, unleash, harness, robust, seamless, holistic, myriad, plethora, cornerstone, beacon, game-changer, paradigm, synergy"。中文同理（writer-skill[11] 禁"说白了/意味着/本质上/换句话说/不可否认"）。
2. **禁止意义拔高**（Wikipedia:Signs_of_AI_writing[18] 之 Words to watch）："stands/serves as, is a testament/reminder, a crucial/pivotal/vital role, underscores/highlights its importance, reflects broader, setting the stage for, evolving landscape, indelible mark"。humanize[8] 同款："No fake significance / legacy / broader-trend padding"。
3. **禁止负向排比**（Wikipedia[18] 单列 Negative parallelisms 一节）："Not only ... but ...", "It is not just ..., it's ..."；文中称其 "stereotypically an 'AI sign'"。
4. **禁止凑三段式**（Wikipedia[18] Rule of three）："LLMs overuse the rule of three ... 'adjective, adjective, adjective'"。stop-slop[6]："Two items beat three."
5. **破折号/花引号硬限制**（humanize[8]）："Avoid em dashes ... Straight quotes and apostrophes only." unslop[9] 给可执行上限："no more than two em-dashes per paragraph. If a sentence needs three, rewrite." masterball[10] 中文更狠："产出正文禁止使用直角引号（「」）与破折号（——/—）。交稿前全文搜索清零。"
6. **"不"字禁令**（masterball[10] 规则7）："任何用'不'来做判断、下定义、划界限的句子结构 ... 凡是先否定再立论的结构，全是AI味。" 示例："不是工具的问题，是用法的问题 → 问题出在用法上"。
7. **空洞结论与套话收尾**（humanize[8]）："No 'In conclusion', 'Overall', 'Ultimately', 'In summary', or a paragraph that restates the piece ... Stop on the last real point." Humanizer-zh[7] §31 给出保留边界："有实际内容的总结、情绪和行动计划可以保留。"
8. **具体名词与系动词**（humanize[8]）："Use 'is / are / has'. Avoid ... 'serves as', 'stands as', 'functions as', 'represents'." "Replace weasel attributions ('experts say', 'studies show') with a named source or concrete detail, or cut the claim."
9. **句式节奏 burstiness**（stop-slop[6]）："Vary rhythm. Mix sentence lengths ... End paragraphs differently." unslop[9] 给量化档："Every paragraph should contain at least one sentence ≤8 words and at least one ≥20 words ... Target sentence-length σ ≥ 6 (human mean ~8.2, GPT-4o ~4.1)."
10. **压缩叙事 → 铺陈**（masterball[10] 规则18）："AI拿到一段经历后，会本能地把它压缩为两三个步骤的抽象叙述 ... 铺陈六要素：时间锚点、工具/版本/出处、原文/原话、情绪变化、思维流与自然行为、外部验证。"
11. **中文特有病灶**（Humanizer-zh[7] F 组）：层叠的"的"、"进行＋动词"、"被字句堆叠"、四字词排比、"随着……的发展"式开头。
12. **标点二次确认**（writer-skill[11] L2-4）："全文是否完全没有冒号、破折号和双引号？（AI容易在修改过程中重新引入这些标点，需要二次确认）。"

## 3. 流程类：先研究／先规划再写作

- **先独立求解，再下结论**（OpenAI 经典战术中文镜像[14]）：原文"首先，你需要自行找出问题的解答，然后把你的解答与学生的解答进行比较……在你没有自行解决这个问题之前，请不要对学生的解答进行判断。" 同页还有"内心独白"四步法，与"询问模型是否有遗漏"（"还有其他相关的摘录吗？请注意不要重复之前的摘录"）。这是"避免过早下结论"的最直接出处。
- **先自建 rubric 再产出**（GPT-5 prompting guide[12]）："First, spend time thinking of a rubric until you are confident ... create a rubric that has 5-7 categories ... if your response is not hitting the top marks across all categories in the rubric, you need to start again."
- **先定探索停止标准**（同上[12]）："Parallelize discovery and stop as soon as you can act ... Early stop criteria: You can name exact content to change."
- **官方写作 skill 的三阶段**（doc-coauthoring[4]）："three stages: Context Gathering, Refinement & Structure, and Reader Testing"；Stage 1 先问受众与影响，Stage 2 每节先脑暴 5-20 个选项再落笔。
- **动笔前强制事实核查**（writer-skill[11]）："在动笔之前，必须先完成这一步。不核查，不写作。" 要求先输出核查报告表，并对存疑／无法核实／口径差异分别给处理规则。
- **SEO/内容稿先 brief 再写**（skillfish content-production[17]）：三模式 Research & Brief → Draft → Optimize，并写 "Rule: If you can't cite a specific number, don't make a vague claim. 'Studies show' is a red flag."
- **记者助手先读三个上下文文件**（digging-agent[15]）："read three context files first ... beat-notes.md / source-list.md / output-format.md ... They are not configuration — they are standards."

## 4. 自检类：输出前的 checklist / rubric 设计

- **五维打分，低于阈值返工**（stop-slop[6]）："Rate 1-10 on each dimension: Directness / Rhythm / Trust / Authenticity / Density. Below 35/50: revise."
- **四层自检金字塔**（writer-skill[11]）：L1 硬性规则（禁用词/标点/结构/空泛工具名，零命中）、L2 风格一致性、L3 内容质量、L4 活人感终审，并输出固定格式"质检报告"；L4 只问一句："读完这篇文章，我感觉是一个有见识的普通人在认真跟我聊一件打动他的事，还是一个AI在给我输出信息？"
- **伦理 + 识读双清单**（med-news-reporter[16]）：Step 4 伦理六问（诽谤／隐私／消息源保护／特殊类别／利益冲突／授权），Step 5 识读六问（标题是否夸张、事实与意见是否混用、是否煽情、数据是否误导、消息源层级、是否披露 AI 参与），并强制在文末输出核对摘要。
- **用"无上下文的读者"真测**（doc-coauthoring[4]）："Test the doc with a fresh Claude (no context) ... Predict Reader Questions → Test with Sub-Agent → Run Additional Checks → Report and Fix."
- **核对"是否新增事实"**（Humanizer-zh[7]）："每个新增的具体信息是否能在原文或用户材料中找到？不能就撤回。"
- **诚实的范围声明**（unslop[9]/humanize[8]）：都写明"不保证通过检测器"，把检测器不可靠写进正文，避免虚假保证。

## 5. 反面清单：被证明无用或有害的做法

1. **"不要像 AI""写得更自然"这类空泛指令。** Wikipedia[18] 开宗明义："this list is descriptive, not prescriptive ... Please do not merely treat these signs as the problems to be fixed; that could just make detection harder." Humanizer-zh[7] 更直接："模式清单是检查线索，不是词语黑名单……单个词、标点、三项列表或四字词不是修改理由。"
2. **穷举词表 + 同义词替换。** masterball[10]："词无限，模式有限。逐词加黑名单必然失败——你禁'稳'它写'顺'。" unslop[9]："Detectors now stack lexical AI-isms, burstiness, surprisal variance ... synonym swaps alone rarely move the fingerprint."
3. **迷信 AI 检测器。** Wikipedia[18]："Do not solely rely on artificial content detection tools ... these tools have non-trivial error rates"；并引 2025 年研究称人的判别力 "no better than random chance"。unslop[9] 点名：Turnitin 2025 年 8 月起用 humanizer 输出重训，此前绕过率数据已过期。
4. **为去味删真实信息（过度矫正）。** humanize[8]："Don't mangle a sentence or delete real information just to purge one banned word." Humanizer-zh[7] 将其列为第一优先级约束。
5. **自相矛盾／含糊的指令。** GPT-5 guide[12]："poorly-constructed prompts containing contradictory or vague instructions can be more damaging to GPT-5 than to other models, as it expends reasoning tokens searching for a way to reconcile the contradictions."
6. **边写边自我审查。** masterball[10]："先把文章写出来，不用在写的过程中顾虑AI味。写完后再逐句通读。"
7. **对推理型模型硬塞 CoT。** unslop[9] 引 Wharton GAIL (2025-06)：在 o 系列／DeepSeek-R1 上 "explicit CoT prompting adds no meaningful accuracy and increases variance"。
8. **把"100% 通过检测器"当验收标准。** humanize[8] 与 unslop[9] 都主动否认该承诺；写进需求只会诱导模型编造或破坏事实。

## 6. 可直接借鉴进本项目提示词的 22 条条款

1. frontmatter 只留 name + description，各一行；description 写"做什么 + 何时触发 + 不用于什么"。[1][5]
2. 主 SKILL.md 只做路由，长范例放 references/ 按需加载。[3]
3. 每条硬规则配一个"允许保留"的反例，防过度矫正。[7]
4. 建"合理化妆扮对照表"，预判模型会用什么理由绕过规则。[16]
5. 禁用词覆盖 AI 高频词与中文套话（说白了／值得注意的是／本质上）。[8][11]
6. 禁意义拔高句式：stands as a testament／标志着新时代／具有重要意义。[18][8]
7. 禁负向排比：不是 X 而是 Y／not just X but Y。[18][10]
8. 禁凑三段式：条目数由信息量决定，不默认三条。[18][6]
9. 破折号设硬上限（≤2/段）；中文正文禁用破折号与直角引号。[9][10]
10. 中文专查层叠"的"、"进行+动词"、被字句、四字排比、"随着……的发展"。[7]
11. 每段至少一短句（≤8 词）一长句（≥20 词），句长 σ≥6。[9]
12. 叙事写"思维流+行为"，保留时间锚点、版本号、原话、情绪变化。[10]
13. 禁无主语归因（专家认为／研究表明）：要么指名来源，要么删。[8][17]
14. 结尾落在最后一个真实论点上，禁"总而言之／拭目以待"。[8][7]
15. 写前强制输出事实核查表，标注确认／存疑／无法核实／口径差异。[11]
16. 未核实内容用 [待查證: ...] 占位，禁止静默补全。[16]
17. 先自建 5-7 维 rubric 并内部迭代，不达标重写。[12]
18. 先独立求解／先列证据，再对给定材料下判断。[14]
19. 输出前分层自检 L1→L2→L3→L4，并产出报告。[11]
20. 五维打分（直接度／节奏／信任／真实／密度），<35/50 返工。[6]
21. 用"无上下文的读者"测试标题与信息差，暴露隐性假设。[4]
22. 明示"不承诺通过 AI 检测器"，把不确定性写进交付契约。[8][9]

## URL 清单

1. https://github.com/anthropics/skills — 抓取时 180,352 star；官方 Agent Skills 仓库
2. https://raw.githubusercontent.com/anthropics/skills/main/template/SKILL.md
3. https://raw.githubusercontent.com/anthropics/skills/main/skills/internal-comms/SKILL.md
4. https://raw.githubusercontent.com/anthropics/skills/main/skills/doc-coauthoring/SKILL.md
5. https://raw.githubusercontent.com/Paldom/node-skills/6dca4a3cd4b3be4e6d6beea7b04c50a9420b5b13/docs/skill-authoring.md — star 未验证
6. https://raw.githubusercontent.com/hardikpandya/stop-slop/main/SKILL.md — 17,902 star
7. https://raw.githubusercontent.com/op7418/Humanizer-zh/main/SKILL.md — 19,362 star
8. https://raw.githubusercontent.com/haidrrrry/humanize-ai-writing/main/PROMPT.md — 16 star
9. https://raw.githubusercontent.com/MohamedAbdallah-14/unslop/main/skills/unslop/SKILL.md — 155 star
10. https://raw.githubusercontent.com/masterball-w/Master-humanizer-skill/main/SKILL.md — 14 star
11. https://raw.githubusercontent.com/guzhezao/writer-skill/main/SKILL.md — 抓取时仅 2 star（方法完整，热度低）
12. https://developers.openai.com/cookbook/examples/gpt-5/gpt-5_prompting_guide.md
13. https://developers.openai.com/api/docs/guides/prompt-engineering.md
14. https://raw.githubusercontent.com/jevantang/chatgpt-prompts/main/docs/guide/tactic-4.md （OpenAI 经典战术中文镜像）
15. https://raw.githubusercontent.com/psubbiah/digging-agent/main/SYSTEM_PROMPT.md
16. https://raw.githubusercontent.com/asgard-ai-platform/skills/main/med-news-reporter/SKILL.md
17. https://raw.githubusercontent.com/johnefemer/skillfish/main/skills/content-production/SKILL.md
18. https://en.wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing
19. https://github.com/blader/humanizer — 抓取时 55,462 star（未直接抓取其 SKILL.md，经 [7] 的"来源"节转引，标注为二手）
