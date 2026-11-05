# 语义阶段 JSON 契约

AI + Skill 按本契约产出紧凑决策；脚本把决策与事实证据编译为完整语义产物，并验证摘要、
路径、ID、枚举和交叉引用。任何验证失败都会整批停止。

运行、搜索、预览、批准、apply 与 undo 统一由
[运行契约](operation-contract.md) 规定，本文件不重复这些操作规则。

## 内容索引

- [批次阶段文档](semantic-contract.md#批次阶段文档)
- [`scan-evidence.json` 与 `hierarchy-semantics.json`](semantic-contract.md#scan-evidencejson-与-hierarchy-semanticsjson)
- [`local-semantics.json`](semantic-contract.md#local-semanticsjson) 与 [语言和关键词](semantic-contract.md#语言与关键词)
- [`remote-semantics.json`](semantic-contract.md#remote-semanticsjson)、[候选种子](semantic-contract.md#candidate-seedsjson) 与 [候选判断](semantic-contract.md#候选判断)
- [查询结果与本地尽力命名](semantic-contract.md#查询结果与本地尽力命名)
- [不可靠判断](semantic-contract.md#不可靠判断)

## 批次阶段文档

每批使用同一份完整 `analysis-input.json`，并按三份核心阶段文件闭环：文件1
`local-semantics.json` 是脚本根据 AI 决策编译的完整本地语义档案；文件2
`search-evidence.json` 是脚本执行并累积的全部搜索事实；文件3
`remote-semantics.json` 是脚本根据 AI 候选判断编译的完整远程语义档案。只有前一阶段的整批文档
通过摘要和枚举校验后，才可进入下一阶段。禁止把同一批拆成逐游戏的本地、搜索、远程闭环。

阶段中断后从该批尚未完成的阶段重新开始；不得混用不同批次或不同搜索运行的文档。
正式 `semantic-preview` 只在整批三个阶段完成后生成；处理中若展示进度页面，该页面
不属于契约产物，不含 manifest 或批准哈希，也不可批准。分批、并发、顺序和计时只
遵循[运行契约的分批与搜索](operation-contract.md#分批与搜索)。

## `scan-evidence.json` 与 `hierarchy-semantics.json`

`scan-evidence.json` 使用 `schema_version=2`，包含扫描根、完整根指纹、
`executable_candidates[]`（兼容字段名；保存候选 ID、EXE/独立 SWF 相对路径、候选目录、本地引擎签名）与
`ancestor_nodes[]`（相对路径、直属文件/目录和 `~` 组织锚点）。它只保存事实。

Skill 必须输出 schema 2、`decision_schema_version=2` 且绑定 `scan_fingerprint` 的层级判断：

- `game_roots[]`：`relative_path`、覆盖的 `candidate_ids`、`type`、0..1 `confidence`、
  非空 `reason`。类型只可为 `single_game_wrapper`、`series_child` 或
  `same_game_launcher`。
- `containers[]`：`relative_path`、`type=series|organization`、`child_roots[]`；系列外层原名
  明确含 `1~2`、`I~III` 等范围时，Skill 另写规范化 `series_range`，否则为 null 或省略。
  只在该容器是子游戏的直接语义父层时（中间可有 `#2` 等 organization 夹层），basename
  中独立的 `@@纯数字` 或 `@纯数字` 才表示“数字部合集”，Skill 必须把容器判为 `series`
  时，AI 只需判断该容器为 series 并提交成员关系；编译器在确认其不包含另一层 series 后
  从原名唯一生成正整数 `series_count`。AI 决策不得填写该字段。带字母的 `@v20418`、
  `@RJ...` 等来源 ID 不匹配；`series_count` 与 `series_range` 互斥。

每个有效启动候选必须被恰好一个最终叶子根覆盖；叶子根不得互相包含。单个 EXE
子目录与说明、存档、补丁等并列时可选择更高包装层；多个独立游戏仍保持多个叶子。
每个叶子用于路径保护的主启动器必须优先选取游戏本体路径中的候选；`汉化补丁`、
`补丁`、`patch`、`update`、修复、工具或修改器目录中的 EXE 仍保留扫描与覆盖事实，但当
同一游戏根存在非补丁候选时不得作为主启动器。`start`、`startup`、`launch`、`启动` 等
启动命名可作为同级候选的优先证据，不能覆盖上述补丁路径排除。
同一容器内本体包与 DLC 包分别具有独立启动项时，两者必须是独立 `series_child`；
共同标题、developer 或 DLC 关系不能作为合并启动项的理由。
`compile-hierarchy-semantics` 只验证这些关系并生成 `analysis-input.json`，不根据具体
名称重新推导层级。

`analysis-input.json` 顶层的 `root_fingerprint` 绑定完整扫描根。最终预览生成前后及
schema v2 apply 前都必须重算；任何新增、删除或修改都会使后续语义和批准码失效。
每个叶子的 `local_fields` 只是脚本从本地证据提取的待审候选，不得直接进入预览或
目标名。它在普通 `version`、`dlc` 之外可包含 `dlc_version` 与
`dlc_range`；前者只在本地明确给出 DLC 版本时填写，后者只在本地明确写出
`DLC1-5`、`DLC I~III` 等枚举范围时填写并统一分隔符为 `~`，均不得从远程字段补入。
`version_tags` 保存本地明确出现并由 AI 复核的版型/附加包标签数组；普通版型逐字保留，
`+AP01-05` 等附加包范围统一为 `+AP01~05`。它与版本/DLC 同属本地字段，不传播。

## `local-decisions.json` 与 `local-semantics.json`

AI 新输入使用 `schema_version=1`、`decision_schema_version=3`。它只写 `game_id`、证据选择、
developer、语言标题与有意义的主副标题、`local_field_decisions`、
置信度、理由和审核状态。`local_field_decisions` 完整覆盖下列字段：

| 类型 | 字段 | AI 决策 |
|---|---|---|
| 单值 | `engine`、`version`、`dlc`、`dlc_version`、`dlc_range`、`positive_tag` | `true` 采用当前候选，`false` 否决；引擎否决后编译为 `引擎未知` |
| 多值 | `version_tags`、`negative_tags` | 写入获批候选值的无重复子集 |

AI 不写 `analysis_fingerprint`、`source_path`、`input_fingerprint`、`raw_name`、叶子
`series_id`、逐成员重复的 `series_common_titles` 或 `reviewed_local_fields`。系列共同标题
改在顶层 `series[]` 按 `series_id` 判断一次；编译器展开到成员。首选查询词默认由脚本对
AI 已确认且逐字存在于当前叶子原 basename 的语言标题执行统一清理；缺少的语种不从
其他来源返回译名补搜。

`compile-local-semantics` 生成文件1。它记录 AI 对该叶子的完整本地判断，不只是 basename
中的标题或标签；同时绑定扫描事实并复制 AI 已采用候选的原始字面值。脚本后续不得绕过
文件1重新解释扫描事实。

顶层包含：`schema_version`（当前为 1）、从分析输入原样复制的
`analysis_fingerprint`，以及覆盖每个游戏且不重复的 `games`。

每个游戏包含：

| 字段 | 内容 |
|---|---|
| `game_id` | 原样复制 |
| `source_path` | 原样复制，禁止改写或相对化 |
| `input_fingerprint` | 复制游戏的 `directory_fingerprint` |
| `raw_name` | 编译器逐字复制分析输入的真实磁盘 basename，用于让文件1自带可读绑定；AI 不填写 |
| `developer_evidence` | `{value,evidence[]}`；无法判断时 value 为 null |
| `reviewed_local_fields` | 编译器根据 AI 的 `local_field_decisions` 与当前候选生成的 `{engine,version,dlc,dlc_version,dlc_range,version_tags,positive_tag,negative_tags}`；下游只消费该对象 |
| `titles.local_segments` | 编译器从无歧义的原始名称定位并生成的完整片段，保留原文 |
| `titles.local_segments_by_language` | 编译器生成普通映射；只有标题重复、交错或自动定位有歧义时，AI 才提供显式 `{jp,zh,en}` 数组映射 |
| `titles.jp/zh/en` | 实际存在的完整标题；不存在为 null |
| `titles.search_keywords.jp/zh/en` | 对应语言搜索关键词；不存在为 null |
| `subtitle_boundary` | 边界及理由；没有明确副标题时为 null |
| `series_id` | 编译器从 hierarchy 的成员关系复制其 ID，否则 null |
| `series_common_titles` | AI 在顶层系列决策审核一次，编译器向成员展开；无法判断时均为 null |
| `english_fallback` | 可选 `{developer,en_title}`；仅在完整路径全 ASCII 且非 ASCII 字段删除后缺少必填值时，由 Skill 提供非空 ASCII 英译 |
| `translation_credit` | AI 可逐字采用当前游戏根 basename 已明确写出的完整署名；用户人工补充可覆盖。不得从内部文件名或说明文本寻找 |
| 分析输入 `local_text_sources` | 受限文本的 `{relative_path,text}`；只供标题、来源 ID、版本/DLC 等其他证据使用，`local_text_evidence` 仅作兼容 |
| `auxiliary_directories` | 新批次固定为空；不寻找或规划直属 CG/存档目录改名 |
| `confidence` | 0 到 1 |
| `reason` | 简短列明实际证据 |
| `review_state` | `accepted` 或 `needs_user_review` |

文件1整体是本地语义的唯一规划输入；其中编译生成的 `reviewed_local_fields` 专门承载本地引擎、
版本/DLC/AP 和正负标签，`developer_evidence`、`titles`、根 basename 或人工补充的 `translation_credit`
与系列字段分别承载其余本地判断。
`analysis-input.local_fields` 只用于向 AI 展示脚本候选和完成契约编译。正负标签候选
只从叶子游戏根 basename 和经确认的系列父子目录 basename 生成，不取内部文件名或说明文本。
AI 可否决
误识别候选（降级为 `null`/`false`/`引擎未知`），但不得无证据增加新值；旧语义中的
引擎值 `未知` 只作兼容输入并规范为 `引擎未知`。缺少复核
对象或键不完整时停止，不得由脚本自动补入 HTML 或目标名。

全英文路径保护投影是唯一例外：文件1仍逐字保存 `引擎未知`，最终纯 ASCII 目标的引擎槽
确定性写作 `ENGINE_UNKNOWN`，使已满足 developer/标题补位的条目可以移动并创建英文投影前完整规范 basename
零字节 TXT。`ENGINE_UNKNOWN` 只是未知状态的兼容占位，不得进入扫描证据、文件1，或被当作
一种已识别引擎。

每个合集还必须在顶层 `series[]` 中写入
`reviewed_surface_fields: {positive_tag, negative_tags}`。`analysis-input.series[].surface_local_fields`
只是候选；AI 提交采用决策，编译器生成复核对象，合集标签传播和目标名仅消费该对象，
不得再解析 `raw_name`。

### `user-supplements.json`

用户人工补充模式使用 schema 1、`mode=manual_supplement` 并绑定当前
`analysis_fingerprint`。顶层固定包含：

- `games[]`：按 `game_id` 覆盖显式 `developer_evidence`、唯一的 `translation_credit`，或按
  `jp|zh|en` 提交 `title_evidence`；每条标题必须同时给出逐字值、理由和来自当前游戏原
  basename 的规范搜索关键词；原名没有该标题时关键词默认填 null，仍可保留人工标题值。
  用户本轮明确要求搜索其提供的完整标题时，该语言可显式设置
  `allow_non_basename_search=true`，并把完整标题统一清理后的文字作为关键词，附上理由。
  例外只对当前指纹和该叶子生效，不从远程译名或相邻游戏扩展。
  可用 `title_exclusions` 按语言记录用户明确否认的标题及理由；排除语种不得生成搜索词。
  人工署名须给出完整值、`group|person` 类型和理由，编译后成为带用户证据的本地语义。
- `candidate_seeds[]`：结构与候选种子 assignment 相同；搜索阶段补齐当前 analysis/local
  指纹后复用既有来源 ID、规范 URL、release 关联和真实回源校验。
- `non_game_candidates[]`：只可引用当前 analysis 的未决 candidate ID，并说明人工确认理由。

编译后的文件1保存规范化 `user_supplements`、`user_supplements_fingerprint` 与
`confirmed_non_game_candidates`。人工标题作为独立用户证据，不要求逐字存在于原 basename；
原名称列仍只显示磁盘 basename。未在补充文件中显式给出的语义不得改变。

### 语言与关键词

1. 先识别完整作品名片段，再标语言；搜索关键词是完整标题的单独派生字段。
   `titles.local_segments` 必须逐项原样存在于当前叶子的 `raw_name`，不得放入翻译、摘要、
   搜索结果或系列传播值。`titles.jp/zh/en` 的每个非空值优先由 `raw_name` 中按原顺序
   出现且不与其他语言槽重叠的字面片段支持；标题被另一语种插入时可由多个有序片段组成。
   当 basename 曾被错误改写时，也可逐字采用扫描证据中真实文件名或受限本地文本明确
   写出的完整标题，但不得拼接、翻译或繁简转换，同一证据标题不得重复归入两个语种槽。
   原文件夹预览仍只显示真实 basename，不得用证据标题覆盖原名。AI 在提交前必须逐字符复核完整 basename，
    对紧贴标题的版本、交错语言、残缺括号、多个并列标签和空格差异分别判断。若主标题
    片段及其语言可逐字确认，先采用该片段搜索，并在理由中说明未采用的相邻内容；
    主标题或语言槽仍无法唯一切分时使用 `needs_user_review`。
2. 假名明确支持日文，拉丁字母标题支持英文。纯汉字段需结合上下文、对译和词义；
   无法可靠区分日中时停止。
3. 不把 developer、版本、汉化组、正负标签或用途说明放进标题。`本体` 仅表示基础包，
   不进入标题；独立 DLC 游戏的 `DLC` 是区分作品身份所必需的标题后缀，必须保留。
   本体与独立 DLC 候选不得仅因共同基础标题而互相匹配；查询与候选的独立 `DLC` 身份
   必须一致。`v/ver/version` 与数字紧贴标题时仍是独立本地版本标记，不要求其前方存在
   空格、括号或其他分隔符；版本片段不得吞入标题或在编译中丢弃。
4. 去除明确副标题边界后的内容，但保留主标题内部标点和区分续作的数字或罗马数字。
5. 结尾括注只有在语义上属于说明而非标题时移除；中间括注默认保留。
6. 每种原文件夹实际存在的语言只生成一次关键词；不得翻译制造缺失语言。
7. `english_fallback` 是英文路径文件系统兼容补位，不是本地语言槽或搜索关键词；
   developer 被使用时脚本追加 `#eng`。其他可选字段不得自由翻译。
8. 规范本地 `.url` 或明确字段中的 DLsite ID、Steam AppID 可直接读取详情并保留为待审
   候选，即使 basename 曾被错误改写而导致标题不一致；这只扩大 Skill 可审证据，不代表
   自动采用。Skill 必须按同一游戏证据采用或拒绝；本地 VNDB ID 仍不能触发直接读取。
9. 搜索关键词必须属于同一叶子游戏对应语言的完整标题；系列共同名不能替代子游戏标题。
   独立 DLC 游戏、续作或版本子包只有保留后缀才能区别于同系列成员时，标题和关键词都
   必须保留该区分词，禁止退化为共同标题查询。基础包删除 `本体`；独立 DLC 游戏在标题
   中保留 `DLC`，格式器不得重复添加 `+DLC`。仅表示本地版本附带 DLC 时，标题不含
   `DLC`，由本地 `dlc=true` 在末尾元数据组输出 `+DLC`。成员原名仅有 `DLC` 等身份词
   而完整检索名来自合集时，不得借用合集共同标题填补成员标题或查询词；应只依据成员自身证据搜索，缺少标题时保持缺失/跳过。父合集和每个成员先独立完成本地/远程判断，搜索结束后才执行系列字段传播；只有接收方日中英标题槽全部为空时才可传播共同标题，绝不回填搜索词或伪造远程来源。
10. 新批次不进入游戏文件夹内部寻找汉化署名或直属 CG/存档目录；仅当前游戏根
    basename 已明确写出的完整署名或用户显式人工补充的单个署名可采用，并保留完整
    字面值。旧回执中已执行的附属目录动作仍按原清单撤销。
11. 搜索关键词统一只保留 Unicode 字母、组合标记和数字；所有标点、审查/缺字代替
    符号及其他特殊字符替换为空格并压缩连续空白。数字、罗马数字与 `DLC` 等身份词
    必须保留，完整标题不改。关键词仍含上述字符、存在首尾或连续空格，或清理后为空
    但条目仍标为 `accepted`，均不符合契约。该规则同时约束 VNDB、DLsite、Steam 的
    首轮与短词补搜查询；完整原始标题只用于证据与回源校验。
    可选 `titles.short_search_keywords` 按 jp/zh/en 给出严格短于同语言首选查询的连续
    原名片段。Skill 选择可区分作品的核心词；脚本只验证字面包含、长度、规范字符和语言槽，
    不自行截断标题。会混淆独立 DLC、续作或分篇身份的短词不得采用。
12. 系列表面 developer 只可作为成员本地证据，不能写成派生后的合集值。制作方传播由脚本
    执行：仅在全部成员搜索成功时，优先汇总逐字一致的最终 developer；最终字段缺失或不一致时，
    仅当成员本地语义 developer 逐字一致才可回退，否则不传播。不得合并别名。共同标题仅可按逐语种唯一且逐字一致的 Skill 审核值在合集与成员
    之间传播，但仅当接收方日中英三个标题槽全部为空；接收方已有任一标题时整组不传播。
    不得由脚本裁剪标题或合并别名，也不得用于搜索关键词。
    外层名称存在
    `1~2`、`I~III` 等明确范围时，Skill 必须在层级容器写规范化 `series_range`，不得把
    范围混入任一语言的 `series_common_titles`。独立 `@@数字`/`@数字` 标记则由层级编译器
    唯一生成 `series_count` 并规范为 `数字部合集`；两字段不得并存；
    合集标题应来自合集自身经复核的本地名称语义，规划器只可按统一格式在全部语言标题后
    追加 `合集`、`合集范围` 或 `数字部合集`，不得从子游戏标题推导。
    合集标签由所有成员 `reviewed_local_fields` 正负标签的精确共同交集，与经复核的
    `reviewed_surface_fields` 合并而成；该集合向成员传播。成员独有标签不得上传或借合集
    横向传播。合集可以显示共同标签。版本、DLC、引擎与汉化组不传播。
13. 提交 `accepted` 前必须反向核对：所有 developer、语言标题都能定位到
    契约允许的本地证据；从 `raw_name` 移除已确认的 developer、日中英标题、版本/DLC
    和正负标签后，剩余有意义的字母、数字或文字片段必须在 `reason` 说明其用途。
    不得以脚本后续可能修正为理由接受漏提、错槽或无证据字段。
14. 正面标签须先识别同一 basename 内的全部明确语义，再做确定性规范化：官中或官方
    中文与步兵、无码、无修、uncensored 同时存在时组合为 `官中步兵`；去码或 demosaic
    是独立语义，须另存 `去码`，不得据此推断步兵。多个语义在紧凑决策的单值
    `positive_tag` 中用空格连接，例如 `官中 去码`；目标名称分别输出 `%官中 %去码`。
    三者同现输出 `官中步兵 去码`，目标为 `%官中步兵 %去码`。不得因文本顺序丢标签。
15. 备用关键词和独立语义补搜接口不属于当前生产契约。现有 `search` 只执行运行契约列出的
    已实现轮次；新增轮次必须先实现脚本、版本化文件2并补充失败测试。

## `remote-decisions.json` 与 `remote-semantics.json`

文件2中的每个候选具有由 `{source,source_id,url}` 生成的稳定 `candidate_key`。AI 新输入使用
`schema_version=1`、`decision_schema_version=3`，每个游戏包含：

- `candidate_decisions[]`：逐个候选只写 `candidate_key`、
  `disposition=adopted|reference|rejected`，可附候选级理由；必须无遗漏覆盖本轮候选；
- `preview_candidate_keys`：同站多候选时按来源写唯一最佳展示 `candidate_key`；优先
  adopted，其次 reference，最后 rejected，同状态按作品身份贴近程度由 Skill 判断。
  该选择只影响日中英轮和来源结果格的展示，不删减任何候选级判断或快捷方式动作；
- `title_assignments[]`：把 adopted 候选的 `title|jp_title|zh_title|en_title` 真实字段映射到
  `jp|zh|en`，只表达语言归槽，不重复标题文本；
- 不再填写 `developer_aliases_confirmed`；先依据标题、身份词、版型和年龄证据
  确认同一精确游戏，确认后不同站制作方即使不同也共同采用；
- `standalone_dlc_title_language`、系列共同标题判断、置信度、理由和审核状态等真正语义判断。

每个游戏的候选级和总理由必须针对本轮文件2重新核对。自由文本只提及本轮确有
候选的来源，不陈述候选数量；候选集合由 `candidate_decisions` 精确表达，未返回候选的
来源由文件2的查询轮展示。编译器只校验这些可确定事实，不从理由中推导候选采用。

系列共同远程标题在顶层 `series[]` 按 `series_id` 判断一次；编译器验证成员集合后把同一
决定展开到完整文件3的每个成员。成员项不得重复填写该字段。

AI 不写候选 URL/ID 对象、`selected_candidates`、`reference_candidates`、
`source_disposition`、`returned_titles`、`developer`、`engine`、`field_sources`、
`audience_classification` 或 `audience_source`。编译器按固定来源优先级和年龄最高限制生成；
同一最高来源仍有多个不同值时停止。

任何实际返回候选的来源都必须完成闭环处置：来源要么由 adopted/rejected
`source_disposition` 覆盖，要么至少有一个可信 reference。远程语义不得遗漏该来源；
`title_exclusions` 排除的语言不得写入 `returned_titles` 或 `series_common_titles`。
多来源候选的年龄分类必须基于原始年龄证据；通过作品身份和标题筛选后若明确
成人版与全年龄版并存，全年龄版不进入 `selected_candidates`，且不得向目标补字段。

编译后的文件3顶层包含：`schema_version`，以及原样复制的 `analysis_fingerprint`、
`local_semantics_fingerprint`、`search_fingerprint` 和覆盖每个游戏的
`games`。

Skill 写紧凑远程决策并用 `compile-remote-semantics` 生成完整字段和三个摘要。
`unlisted_game` 只能显式提供 `confidence` 与 `reason`，编译器把未列出的游戏
补为不含任何候选或字段的 `needs_user_review`；它不能代表默认采用。

### `candidate-seeds.json`

用户在当前任务明确提供 VNDB、DLsite 或 Steam 详情链接时，Skill 可在 `search`
之前建立 schema 1 的候选种子文档。顶层必须绑定 `analysis_fingerprint` 与
`local_semantics_fingerprint`；`assignments[]` 每项包含 `game_id`、`source`、
`source_id`、来源规范详情 `url`、非空 `reason`，并可保留 `provided_url`。

仅当用户明确指出同一个 VN 被拆为多个具有独立游戏目录的 VNDB release 时，可在对应
`provided_url=rNNN` 的 assignment 中增加 `release_title_evidence`。按 `jp|zh|en` 每个实际
提供的语种记录 `{value,reason}`；脚本只在逐项回读 release、验证其关联的 `vNNN` 后把该标题
附加为该 release 候选的标题证据。该证据只作用于绑定的叶子游戏，不改变普通 `vNNN` 单作或
各子游戏分别对应不同 VNDB `vNNN` 的合集。父合集使用成员经审核的共同标题，并可保留共同
`vNNN` 快捷方式；每个成员仍使用自己的 release 快捷方式。生产脚本不得按具体 VN、系列或 ID 特判。

若 VNDB 的 `provided_url` 是 `rNNN` release，生产客户端须读取该 release，确认它关联
同一 `source_id=vNNN` 后才可使用其标题区分同一 VN 的分篇；规范 URL 仍为 VN URL。

脚本验证叶子 ID、来源 ID 格式和规范 URL 一致性，只把该 ID 注入指定游戏的对应来源；
直接 ID 仍必须真实读取详情。种子文档原样进入 `search-evidence.json`、搜索指纹和
日期日志，不得写入生产默认值，也不得跨搜索运行或本地语义复用。Skill 对显式详情
完成同一性判断后，远程采用仍遵循下述字段优先级与年龄证据规则。

本地扫描发现的 VNDB ID 不属于候选种子，不得触发直接 ID 查询；VNDB 自动发现只按
本地语义中的标题关键词进行。三轮候选发现、错误重试与回源验证只遵循
[运行契约的分批与搜索](operation-contract.md#分批与搜索)。

每个游戏包含：

| 字段 | 内容 |
|---|---|
| `game_id` | 原样复制 |
| `selected_candidates` | 已确认同一精确游戏并用于名称同一性佐证或补充缺失语种的 `[{source,source_id,url}]`；逐项存在于搜索证据 |
| `reference_candidates` | 非精确命名采用项、仅作相关版本/标签参考的候选身份；逐项存在于搜索证据且不得与 adopted 重叠 |
| `returned_titles` | 采用来源真实返回的 `{jp,zh,en}`；未返回为 null |
| `propagated_title_context` | 仅当接收方日中英标题槽全空时，记录搜索结束后填入的 Skill 审核共同标题；不改写搜索证据、`returned_titles` 或 `field_sources` |
| `standalone_dlc_title_language` | 可选 `jp|zh|en`；独立 DLC 在远程标题合并完成后，把本地证据中的 `DLC` 追加到该语言标题，末尾元数据不再写 `+DLC`；该获批转换也用于判断对应来源返回字段是否贡献最终标题，但不得改写搜索证据中的返回原文 |
| `developer` | 采用来源返回值；无可信值为 null |
| `engine` | `{value,source}` 或 null；只作远程原始审计，不进入预计名称 |
| `field_sources` | developer、engine、jp、zh、en 的远程审计来源；engine 不进入预计名称 |
| `source_disposition` | 每个审阅来源为 `adopted` 或 `rejected` |
| `audience_classification` | 已采用候选具备年龄证据时必填：`all_ages` 或 `adult`；多来源时按有证据采用候选的最高限制汇总，成人优先 |
| `audience_source` | 年龄证据承载候选的 `{source,source_id,url}`；只可指向已采用候选 |
| `confidence` | 0 到 1 |
| `reason` | 同一性、冲突、语言槽和采用依据 |
| `review_state` | `accepted` 或 `needs_user_review` |

### 候选判断

1. 先判断是否同一游戏，再决定字段。任一日中英标题规范化包含只是必要证据，
   不是自动采用。
2. 显式来源 ID、官方详情、精确标题和版本身份优先；DLC、移动版、推荐区
   和同名异作拒绝。
3. 多来源属于同一精确游戏时共同采用，即使制作方字面不同。先完成同作与年龄判断，
   然后只在共同采用组内比较制作方：字面不同时优先非英文文字名称，同类文字再按
   VNDB、DLsite、Steam 取值。日中英标题仍按站点优先级逐字段取值；先判断哪些已选
   来源为该语言槽提供值，再取最高来源，不得因为偏好低来源的不同译名而绕过优先级。
4. 同一最高优先级来源内 developer 或同语言返回冲突时标 `needs_user_review`。
   本地制作方不参与远程候选取舍或最终 developer 选值；Skill 只依据标题、版型、
   分篇、本体/DLC、年龄及其他作品身份事实审核异作。异作候选逐项说明未采用原因并标
   `rejected`，不得降为 `reference` 继续参与名称；不能唯一判定则标 `needs_user_review`。
5. 本地 developer 与单一可信远程 developer 不同不再单独阻止采用；保留冲突审计，
   预计名称使用远程值。
6. 搜索关键词只说明搜了什么，绝不等同于数据库返回名称。
7. Skill 可把候选任一真实标题纠正到合适的日中英槽；返回文本仍必须逐字来自已选
   候选，必须保留原字形，不得繁转简、简转繁、翻译或改写制造新标题。
8. `reference_candidates` 不参与 developer、engine 或日中英字段来源；它只能参与
   最高远程版本、最佳远程正面标签及真实详情 `.url` 规划。已确认是同一精确游戏且用于
   最终采用组内名称同一性佐证或补充缺失语种的候选必须进入 `selected_candidates`，
   不能降为 reference。制作方不同本身不得作为不采用理由。
9. 年龄分类只能使用搜索证据中来源实际返回的最低年龄、商店分区或成人标记。具备
   此类证据的已采用候选必须显式审核全年龄或成人；多来源合并时成人优先，全年龄在目标名称中作为具体
   负面标签 `#来源全年龄`。已采用候选的来源取编译后的唯一 `audience_source.source`，
   仅本地审核标签取 `本地`；本地与远程均全年龄时只保留远程来源标签。未知状态不得写成
   `#负面标签` 或其他泛化标签。
10. 返回标题中的语言版本说明保留在原始搜索证据中；目标标题删除这些说明，预览在
    搜索结果中把被删除片段标红。该显示转换不改变搜索证据或候选身份。
11. 通过验证的 `developer`、`returned_titles` 与 `field_sources` 是预计名称的唯一远程
    语义输入。`accepted` 且 `selected_candidates` 非空时，规划脚本必须先清空本地日中英
    标题，再只写入 `returned_titles` 中逐项具有来源的非空值；远程缺失语种不得由本地
    Skill 字段、文件证据、合集表面或系列传播补缺。不得遍历候选重新选择、补缺、改槽
    或改变来源。
12. `selected_candidates` 中多个可信来源可返回重复或不同的名称字段；制作方按上述
    共同采用规则选值，日中英标题仍严格按 `VNDB > DLsite > Steam` 逐字段取唯一值，
    各字段记录唯一 `field_sources`。未被选为最终值的
    低优先级返回只用于同一性佐证，不得由脚本二次合并或覆盖。

## 查询结果与本地尽力命名

- 只有 `review_state=accepted` 且 `selected_candidates` 非空才算查询成功。
- 查询成功时，最终 developer 与日中英标题只来自远程语义；远程未返回的标题语种必须为空，不得残留本地标题。全体子游戏查询成功的合集也不得从本地系列共同标题回填，只能使用经审核的远程共同标题。
- 没有最终采用候选而本地语义为 `accepted` 时，远程判断仍保持
  `needs_user_review` 且不得制造字段；规划脚本改用本地引擎、标题、版本、DLC、
  人工补充的汉化署名和根/系列目录标签尽力命名。独立游戏移入 `~查询失败/`，
  已确认的合集及其成员整体放入扫描根 `~合集/`，合集保留失败标记。
- 上述任何保留本地标题的可执行条目，其 developer 都必须在方括号内追加
  `#uncheck`；本地也缺少时使用 `[unknown #uncheck]`。可信 reference 不能免除该标记；
  已有候选但同一性或同一最高优先级来源的 developer 字段冲突时仍保持跳过。
- 远程引擎、版本、DLC、汉化组及正负标签均不得进入预计名称 metadata。

## 不可靠判断

`needs_user_review` 条目仍复制 ID、路径和摘要，保留已确认的证据，并在 `reason`
写明具体冲突。不要填推测值、目标名称或文件操作。
