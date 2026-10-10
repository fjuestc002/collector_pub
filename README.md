# 收集器 · dataset collector

独立项目：多 PDK MOS 器件数据集采集器，服务 MIDAS 优化器（不嵌入其运行流）。
主链 = 自产 spectre deck → ssh 直跑批处理仿真器 → IRIS 直解读数 → CSV 表。

## 发表安全（红线，10-09 用户裁定）

- **仓库零凭证、零 PDK 指纹**：发行物不含任何真实 PDK 路径/器件名/主机地址/口令。
- **凭证三级**（`tools/vm_run.py::connect`）：`password_env` 环境变量 → 运行时交互输入(不留盘) → SSH key+agent。无硬编码缺省密码。
- **不探测 license 文件**：并发上限 = 用户申报(profile.license.max_concurrent_spectre)+为同事实务预留 reserve+日志被动观测排队→阶梯回退。
- 真实 PDK 画像住 `profiles_local/`（gitignore），发行仓只带 `profiles/EXAMPLE_generic_*.json` 虚构样例。
- `redline_scan` 全仓指纹扫描闸（M4-4d 落地 `tools/redline_scan.py`）：纪律=**跑在 git add 之后**，
  扫描集=git 跟踪面∪暂存面（未 add 不可见，时序漏防堵死）；词表=内置公共厂商名+本地清单；
  收集器仓比 demo 更严——说明面叙述性提及也不允许（发行物零指纹）。另含口令字面/KEY 块/
  license 探测引用/私网 IP 探针与 .gitignore 忽略护栏。数据落 `data/`，整目录 gitignore。

## 目录结构（M0+M1 已就位）

```
collector/
  profiles/            可发行虚构画像样例（换 PDK=加画像+冒烟判别，内核零改码）
  profiles_local/      [gitignore] 真实 PDK 画像与金标准
  collector/           包：profile.py 画像装载校验 / deck_gen.py 曲线 deck 生成 /
                       engine.py 执行引擎(M2) / remote.py 凭证三级 / qc.py 质检闸(M4)
  cli.py               命令行入口 deck/plan/run/qc（M5 后并入 GUI）
  tests/               离线金标准 + 发表红线自检 + 真 PDK 集成(skip-if-absent)
  ledgers/             QC 指纹台账(append-only JSONL)——只存指纹/计数/状态零物理数值，
                       此目录本身可进 git（红线锁的是数值，不是审计链）
  data/  out/          数据与运行产物（不入库）；out/queue/densify.jsonl=加密待采队列（4c 产物，M5 消费）
```

## 并发与资源（M2 缺省策略，此处备案）

- 执行体=多进程单线程仿真器短命枪；job 间并发是本仓唯一需要的"并行 job 数"层。
- 缺省 N = 逻辑线程 − 预留（用户实测 16 线程/12 job 有效档）；每包三探测（核数/内存/磁盘 +
  `+query=all` 官方预算 + 首包实测 RSS）动态重算，运行期滑窗回退：吃紧⇒停派新枪不杀在跑。
- 防死机三件套：每包 ≤2000 枪、包间清远端 .raw/psf、deck 附带磁盘水位暂停开关（M2 接线）。
- GUI 启动前强制弹"将占用 N 核/约 X GB 内存，影响本机其他工作"确认窗。

## 待实机验证（诚实账）

1. ~~多分析形制 `V1#dc`~~ **已判死**：判别实跑报 `SFE-691 Illegal parameter name 'V1#dc'`（解析期拒，健康闸拦下）。
   改道=**多实例单分析形制**：一 deck 多器件拷贝、每拷贝一条独立漏压源(锚点固定)、栅源共节点一次扫——
   同时实现"全 W 一 deck"省发射。~~候判别枪~~ **已判活（10-09 暮，见下方"多实例判别"）**。
2. ~~负极性(p 管) deck：按极性规则生成+离线金标准测过，未在实机跑过。~~ **已销**：负极性单锚实跑 0 errors；
   符号约定实测定案 **gmoverid ≡ gm/|ids|**（p 管 ids<0、gm 恒正）。
3. ~~磁盘水位暂停开关取值单位：官方帮助截断处~~ **已标定（10-09 实机 `-h` 原文+AB 双枪）**：`+disk_check` 阈值=**字节**
   （"less than N bytes，例 1e10"）。引擎对外按 MB 入参、入令 ×1e6 换算。行为实证：阈值 600000MB(>盘剩) ⇒ spectre
   空等 59.8s(CPU 仅 0.7s, util 1.18%) 直到 watchdog 杀；阈值 20000MB(<盘剩) ⇒ 正常跑完 RC=0——
   **suspend-wait 语义坐实，watchdog 必须同时兜住它**。挂机跑建议开（如 20000=20GB 线）。
4. ~~IRIS sweep 列自动识别对**降序**扫段失配（p 管 0→−5 报 WARN，本轮靠 g 节点列兜住）~~ **已修（10-09 M4-4b，v0.4）**：
   判据改"单调(含降)+端点贴合"。实证附赠：PSF 降序件的 `sweep min/max` 字段系**起/止值**（实测 min=0.0
   max=−5.0）而非算术 min/max ⇒ 端点式对升降两向同形，仅方向判据须接受非递增。零扰动核验=新旧 CSV
   数值区逐位一致、唯一变化 col26→sweep；镜像 gsieve 同字（该侧 ocn_probes 目录系工作树件未入库，
   镜像闸只校文件本体）。
5. I1 在粗格（vstep=0.1）亚阈区呈离散化尾巴：中心差分弦差/局部导比≈sinh(h)/h，
   实测 p90≈0.99 而 median 1.4%——**物理+步长伪影非映射错**，median 判级不误杀（10-09 实测
   数据自证）；正式采集建议 vstep≤0.02 + `--charge`（否则 I4 电荷交叉整批跳过呈报）。

## 已实机自证（10-09，真机数据住 profiles_local/out，不入库）

- 单锚金标准链全通：画像渲染与 VM 实跑金标准 deck **逐字节对齐** → 批仿真 502ms/0 errors/峰值 91MB →
  IRIS `.raw/dc.dc` 13KB → `iris2csv` 解出 51 行 11 向量 → **哨兵 gm/ids≡gmoverid 最大相对差 5.3e-12**。
- 单枪 license 开销 118ms(23%)，短命进程形制成立。
- **多实例判别（M1-v2 形制判活）**：3 实例×2 漏锚×2 W 一 deck（458ms/0 errors），单 `dc.dc` 内
  21 列器件向量**全部按 `M1:`/`M2:`/`M3:` 实例名正确拆开**（IRIS 多向量未知位销案）。判据四连：
  ①**解耦哨兵**——组内实例与历史单跑曲线**逐位一致(0.0e+00)**，理想压源零耦合假说成立，形制定案；
  ②W 比 ids/gm/gds/gmb 中位 1.89~1.97（≈2 带窄宽税），vth 等宽参数 1.8%~4.2% 漂移带=窄宽真物理非串列（口径承 G7/I5）；
  ③锚点区分——同 W 不同漏压 ids 最大差 7.3e-5 A，vds 轴生效；
  ④每实例独立 gm/|ids| 哨兵 3.5e-12~8.1e-12（含 p 管）。
  判据脚本 `tools/check_disc.py`（样本目录中性命名；跟踪文件全仓指纹扫描 CLEAN）。
- p 管首跑实机通：负极性 0→−5，ids<0/gm>0 sanity 过。
- **M2 引擎实弹（10-09 深夜）**：`cli.py run` 4 片包（2L×2 漏锚，每片内含 2 W）并发 N=4 探测档
  （8 线程−4=4 绑定；license 申报 12 不绑；内存 64 枪位不绑）全到账，单枪均值 0.57s；
  包间清远端产物实证（批量件清零，手工旧件留存）；**账本重放幂等**——复跑显示"已到账 4 待发 0"零派发。
  实机踩坑入册：探测命令自带 awk 单引号，经 `bash -lc '…'` 二次包裹被打碎 ⇒ `measure` 改走 `run`
  （nproc/free/df 无需登录环境）。离线 29 项测试（FakeRemote 全链：计划/三层预算/账本五态/健康闸/
  背压停派不杀在跑/阶梯回退/watchdog 超时/清理令）。
- **M4-4a 质检闸实跑（10-09 深夜，对 M2 实弹回样 4 片只读核验）**：装载 4/4，五闸行为符合设计——
  哨兵 gm/|ids|≡gmoverid max 6.6e-12；I1 median 1.44%（尾巴见"待实机验证 5"）；I2 median 14.3%<30%
  （1V 锚距弦差，曲率呈报口径）；I5 gmoverid 0.19%/av0 1.0% 在窄宽真物理带内不点火；I4 因该批未带
  `--charge` 整批正确跳过并呈报；verdict=WARN。离线 41 项（含六类埋雷必炸：串列/整列跳位×4/缺片/
  partial 降级/NaN 残片/指纹撞车/行数不齐 + 电荷交叉/ft 恒等/measured 申报不符响亮拒）。
  **判级粒度=片级/锚对级**（单坏片 median 不被邻片稀释、坏片点名进待补采队列——demo 全局 median 口径
  在收集器场景会漏检，开发中实测纠正入册）。
- **M4-4b 解析器降序修（10-09 夜）**：p 管 0→−5 复现件 v0.3⇒rc2/WARN、v0.4⇒rc0 且 sweep 列自动识别
  （=g 列逐位等、严格降序）；三支升序真样回归 rc0 不变；零扰动=51×12 数值区逐位一致仅列名变；
  双仓同字镜像逐字节校验入测。离线 51 项全绿。
- **M4-4d 台账+红线扫描器（10-09 深夜）**：`cli.py qc` 默认追加 `ledgers/qc.jsonl`——
  一 run=1 行 qc_run+片数行 qc_deck（ts/指纹/行数/状态/旗标名，序列化前递归禁 float 自守），
  实弹回样 5 行零浮点核验过；哨兵判级同步上收片级（坏行列点名片，4a 粒度发现的收尾）。
  `tools/redline_scan.py` 三层=指纹硬零(跟踪∪暂存面,纯字母数字词边界)/凭证与探测面/忽略护栏，
  留档 JSON 限 out/；仓内自扫 18 文件×10 词 CLEAN，"脏件 add 前不可见 add 后必见"入测。
  离线 69 项全绿。
- **M4-4c 插值误差闸（10-09 深夜）**：`collector/interp_check.py` + `cli.py interp-plan/interp-check`。
  参考口径=沿 vds 的 signed-log 线性插值（与生产 log 插值族同形；本闸约束格密度量级，
  不要求与生产插值器逐位等——生产还含 vgs/W 向与 logJ 主键，另一面账）。判级=实例×锚对
  median（承 4a 粒度裁定+I2 稀释教训，队列按实例点名）；阈值 gm≤1%/ids≤2%（画像
  `qc.interp` 覆写、CLI `--gm-tol/--ids-tol`，GUI 可调候 M5）。三片 vgs 向量错位=机器判
  （加密批 vstep 须与粗格一致，错位呈报不硬炸）。膝区专项的化解：中点插值误差≡对数域
  二阶导三点差分——每 (pair,fet,L) 按锚段弯曲排序呈报（膝随 L 走，R-b 教训入工程），
  不设魔法阈值：只进报告，红牌与下队权留给误差闸（干净网格不刷队列）。超差自动出加密
  待采队列 `out/queue/densify.jsonl`（跨度+实例+三片牌名；派生统计住 out/ 不进 git，
  台账收编口径候 M5）。**自首：显著段过滤双重除法**——4a 的 I5（及 4c 初稿同抄）把已归一
  ids 再除 _W_m，阈值被除 W 两次、过滤形同虚设；零样本复现件当场炸出，两处同修，修后对
  M2 实弹回样只读 qc 回归裁决不翻（仍 WARN、I5 带内）。离线 85/85。实弹半边候凭证：中点
  加密 2 枪（`out/plan/densify_m2.plan.jsonl` 已生成）+`--charge` 合 6 枪一跑（顺带完成
  4e 的 I4 电荷交叉实弹）。

- **M4-4c/4e 实弹（10-09 深夜，git c0fb071，全仓 87/87）**：`--charge` 粗格 4 枪 +
  中点加密 2 枪全中（`data_charge/` 独立账根，单发烟测 2.4s）。interp-check 实弹首跑
  闭环真转：**ids 轴 median 0.44~0.47% 过关；gm 轴 L1u 0.969% 险过、L2u 1.01% 触线
  0.1% 红牌 ⇒ 点名实例 M1、自动下队 `out/queue/densify.jsonl` 2 行**（span 1.5→2.5，
  两 L 各自担）。阈 gm_tol/ids_tol 画像可覆写，贴线该不该采候 M5 人裁。
  **实弹暴露三案（自首+修毕）**：① cmd_interp_check 漏 `/pair` 层——离线测试扁平布局
  没照住生产 `data/<pair>/<deck_id>` 真形，补 CLI 级 subprocess 回归锁死；② qc 台账
  run 行曾带 profile 原名 ⇒ 真实 PDK 名被红线扫描器**自咬**（污线仅在工区、历史净），
  改 sha16 摘要制 + 回归测试；③ `data_charge/` 不在 .gitignore 罩内，补 `data_*/`。
  **I4 电荷交叉实弹判词**：数据无罪——qg+qd+qs+qb≡1e-26C≈0 且全扫不漂（本征端子电荷
  族自洽）；`cgg − d(qg)/d(vgs)` ≡ 0.888fF(W=1µm)/1.763fF(W=2µm)，严格随 W 线性、
  散布 0.002% ⇒ PDK 子ckt 壳的几何寄生栅帽签名。**去常数后 relerr 中位 0.01~0.02%**
  ——现行"严格恒等"前提只在裸模型成立，带寄生壳单元上把寄生电容当误差烧。闸重设计
  （去常数判残差，副产品=每(器件,W)寄生栅帽估计，对建库是可用数据）候裁后动。

- **M4-I4 闸 v2（10-10 裁定，git efd253a，全仓 90/90）**：电荷交叉双层判据——
  flat 层（系列近常数）裸恒等硬闸（v1 向后兼容，常数串列照抓）；shell 层（PDK 壳）
  逐实例去常数 b=median(c−fd) 后 qg_cgg 残差硬闸、qd_cgd 主判换 Spearman rho≥i4_corr
  趋势硬闸+残差软呈报（耗尽调制面不硬判恒等）。实弹复验同批数据：qg 残差 median
  0.016%/max 1.4%、rho 0.9999/1.0、总判词 FAIL→WARN（只剩两 V1.5 片 I4qdS 软呈报）；
  副产物寄生栅帽估计 b=0.888fF@W1µm/1.763fF@W2µm 随 W 线性，只入 out/ 报告，
  台账零浮点复验 15 行 PASS。p 格幅值口径自咬案由既有测试当场抓获（v2 初稿丢 abs）。

## M5 闭环调度 + GUI（10-10 三段交付，实弹首转收官）

**闭环形制**：`interp-check` 超差落提案（`out/queue/densify.jsonl`）→ `cli.py drain`
列账 → 人签字（`--adopt` 选靶 / `all` 全收；`--dismiss` 必带理由）→ 拆锚发射
（跨度每轮只劈一层、最大跨先劈；`min-gap 0.05V` 防无限加密、`max-slots 64` 预算
封顶超出顺延、`max-anchors 4` 每提案每轮上限）→ qc 新片 + interp 复验 → 事件账
销案（`out/queue/events.jsonl`）。`--rounds N` 无人值守连转；GUI（`gui.py`，PySide6
可选依赖，核心库零 Qt）同径复用 loop.run_drain：选行签字、阈值面板、证据条、
滚动日志，发射在 worker 线程不冻 UI。

**复验的渐进语义**：提案兑现=新锚真仿到账（真值不需再验）；其质量下一轮检验——
已到账锚升格为粗格（**外来片现场反推**：实弹的中点 2.0 不在任何 drain 计划里也
自动升格），本轮新锚作探针，相邻中点有片才对照。

**幂等三保险**：事件账状态 + 累计加密计划（去重键 deck_id）+ 数据盘覆盖扫描。
同一提案重复 drain 不重火；发射半途崩 ⇒ `fire_incomplete`，下轮**欠账优先补火
不再拆层**（首转实测：paramiko 缺位崩在 connect，账本自留欠账，装包重跑自动补火
4 枪——事故即判据）。

**实弹首转（10-10，账上两枚 L1u/L2u 提案）**：干跑列账 2 件 open → 全签发射
四分位 1.75/2.25（4 枪 ×4 并发 5.1s）→ qc 新片 WARN（两块 V1.75 片 qd/cgd 去常数
残差 0.057/0.090——I4 v2 定案的耗尽调制软呈报面，非红牌）→ **interp 复验 4 中点
PASS：半跨 gm median 0.08~0.57%、ids ≤0.23%，原 1.01% 红牌跨经加密后插值干净**
→ 两案销账、新提案 0、台账 +5 行零浮点复验通过。闭环一圈真转，无需人工轮值。

**红线自查**：GUI 不打凭证、不写 git 面画像（`profiles/` 拒写）；事件/计划账住
out/ gitignore 区；发射路径唯一（run/drain/GUI 共用 collector.fire）；纪律闸测试
盯核心包 import 面零 PySide6。

## P4 闭环三件套（10-10 "把 P4 做完"令，设计文档 §2/§3/§0）

**P4.3 域申报（domain）**：画像 `pairs.<pair>.<fet>.domain` 块——`L_range/W_range/
vds_range/vgs_range`（长度可带 130n/1.2u 后缀，装载即归一米）+ `vbs_topology`
（tied=衬底接源 / body=独立体）+ `source=manual`（人工填写定案：PDK 方言穷不尽，
`soa` 留位拒实现）。pack 三件事：**灯一硬闸**（覆盖轴越出申报域 ⇒ 响亮拒，
"申报 ⊇ 覆盖"是数据集安全底线）；差额块（申报−已覆盖的边块 + body 申报但表只
vbs=0 的接法块）写进 `manifest.devices.*.gap_blocks`；domain 原样入册。优化器
`GsieveProblem` 构造期把喂进 `l=`/`w=` 的搜索变量夹紧到申报域（绑定证据=表达式
token×params_used 交集，不按变量名猜；未申报=位级零扰动）。

**P4.4 三源回流并轨（单队列不开第二循环）**：优化器 run 尾自动读自家
`sieve_ledger.csv`（FAIL:out-of-domain/nonphys 行）与 `verify_ledger.csv`（金对子
超差行），按网表表达式复原几何 → `<run>/backflow.jsonl`（聚合计数、同块不重复）。
收集器 `cli.py ingest --profile … --file backflow.jsonl`：model→pair/fet 走严格
同名链反查；vds 轴块进 `densify.jsonl`（origin=domain，与 interp 提案同键制同
幂等律，run_drain 零新码直接消费）；L/W/vgs/J/vbs 与残差整点进 `gap_blocks.jsonl`
候块账——**n 倍代价何时付由签字环节定块，机器不代裁**。interp 提案行现带
origin=interp（旧行缺省视为 interp），drain 列账按 `[origin/gate]` 呈报。

**P4.5 达标判据单**：`cli.py scorecard --manifest <表目录> [--interp … --verify …
--backflow … --ledger ledgers/qc.jsonl --queue …]` 出四栏（域覆盖/interp 误差分布/
金对子残差 Spearman Sp/五闸通过率）+三盏灯（申报⊇覆盖 / interp 红牌清零 / Sp 过线），
全绿⇒呈"可冻结 vN（表指纹）"。**不设 PM 栏**（用户裁定原话存档："PM 的不确定性，
是爹妈生出就有的"）；零新物理量计算，全部数字读自现成台账；缺源如实"无账"，
灯不冒充绿。

判别测试 `tests/test_p4_domain_scorecard.py`（13 条）；优化器对侧
`tests/test_domain_backflow.py`（11 条）。接口事实同步
`optimization_tool\docs\PDK表目录约定.md` §7/§8。

## 发布说明（2026-10-11，公开仓）

**定位**：本仓是"MIDAS 数据收集器"（PDK 器件数据表采集端），与"MIDAS 优化器"
（披沙物理信息代理，二进制发行见 optimization_tools_run 仓）配套：收集器产数据表
→ `pack` 入库 → 优化器按"表名=器件名=PDK 模型名严格同名"链消费。闭环机制
（表版本闸/表锁/域申报/回流/达标判据单）见 README 上文与 `cli.py --help`。

**仓库红线（自证零残留）**：本仓零 PDK 数据、零厂商标识、零凭证、零 license
探测。真实 PDK 画像一律放 `profiles_local/`（gitignore，本仓不含）；采集数据、
事件账、报告全部住 `data*/ out/`（gitignore）。`ledgers/qc.jsonl` 是零物理
数值的指纹台账（只含 sha/计数/状态），可公开。历史=单提交干净起点；红线扫描器
`tools/redline_scan.py`（纪律：跑在 git add 之后）纳入 CI 习惯即可复用。

**依赖与许可**：核心包纯标准库；`numpy`（BSD-3，pack 建表数值面）、`paramiko`
（LGPL-BSD，远端采集）、`PySide6`（LGPL，仅 `gui.py`——核心包 import 面有测试
闸钉死零 PySide6）为运行时第三方件，各依其自身许可，本仓不随带其源码。
第三方代码的再分发许可义务（如有）由使用者随其发行版自行履行。

**许可与免责**：本仓自研代码 **all rights reserved**（有意不提供 OSI 式许可
文件，与优化器发行口径一致）。软件按"原样"提供，无任何明示或暗示保证。
使用者对其连接的目标环境（仿真机、EDA 许可、PDK 许可条款）合规性负责——
本工具不内置任何许可探测，也不替你判断 PDK 数据可否外发。

**上手**：`python -m unittest discover`（离线测试，无厂商画像即自动跳过相应
用例）；跑真 PDK 从 `profiles/EXAMPLE_generic_mos_spectre.json` 复制改写你的
`profiles_local/*.json`（把该文件加进 `profiles_local/fingerprint_terms.txt`
即让红线扫描器带上你的私有指纹词）。
