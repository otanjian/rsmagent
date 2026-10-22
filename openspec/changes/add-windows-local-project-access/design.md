# Design

## Context

截至2026-10-15，通用选择/绑定、原生授权、设备WSS、journal、版本及来源已有实现；Windows缺少文件helper和隔离脚本启动器。第一阶段补有限文件操作，第二阶段补Windows脚本/兼容技能；第一阶段不等第二阶段，整个change完成需要两阶段证据。不把POSIX标签视为脚本已可用，不将记忆锁故障当成执行逃逸证据。

| 现有接入点 | 复用及缺口 |
|---|---|
| `desktop/native/fs-guard/src/{main,paths,grants,ops,protocol}.rs` | 私有帧/取消/有界访问可复用，当前unix描述符及libc实现需平台分层，只读白名单不能直接扩为写入 |
| `desktop/src/main/local-files/{selection,grants,bind-context,fs-guard,fs-guard-binary}.ts` | 复用选择/活动grant；开发态构建候选未带.exe，打包候选为resources/fs-guard且先做accessSync检查，不能假设自动补后缀，须统一产物名后真包验证 |
| `desktop/src/main/remote/{binding-setup,remote-container-ipc}.ts` | 保留choose→设备→workspace→binding→grant确认顺序，未确认不显示就绪 |
| `desktop/src/main/project-browser/browser.ts` | 当前读走只读helper，保存走v2执行；新增文件专用路由，不能只修面板绕过模型链路 |
| `desktop/src/main/project-execution/{command-runner,device-execution,journal,file-versions}.ts` | 复用串行、意图、回执；现有版本检测仅告警仍执行，新版Windows需写前阻断，不能直接复用冲突语义 |
| `auth/desktop_contracts_v2.py`、`integrations/desktop/{gateway,execution_broker,execution_capability}.py` | 复用执行信封和已有files/scripts能力面；调整平台能力表达并验证版本兼容，不能从现有开关推断授权 |
| `desktop/src/main/local-execution/{interpreter,launch,worker-session}.ts` | 已有冻结worker与探测，比较复用和独立CPython的隔离资源、依赖及维护成本，再选择运行时 |
| `agent/workspace/execution_target.py`、`agent/desktop_remote/mode.py`及工具派发接缝 | 保留read-only reference不是execution grant；按统一合同内的文件能力派发本机五工具，不能被原v2拒绝后落回服务器 |
| `.github/workflows/release.yml`、`desktop/electron-builder{,.win}.js` | 当前仅mac分支构建/注入fs-guard；Windows缺helper构建及extraResources配置，须补两个入口的构建、签名、打包和路径验证 |

主规范依据为scoped-project-browser、desktop-tenant-context及既有授权/审计/配额规范。承接harden-windows-desktop-reliability的evidence/deferred-windows-execution-change.md所记录的平台缺口，旧记录中的候选契约调整以本次D4为准。本change不重复harden的四份通用执行规范。harden与add-shared-agent-release-governance均MODIFIED execution-isolation的“权限模式在租户语境下收紧”；该capability须串行同步，每次以已更新主规范核对剩余delta，保留双方及本次Windows场景，不整文件覆盖。不宣称全仓无重叠，也不等待其他change全部实施完才开展本次原型。

## Goals / Non-Goals

**Goals:** 在本机和远程桌面中完成原生选择→明确只读/读写→模型五文件工具→本机真实文件/面板的闭环；Windows 10 22H2/11 x64、NTFS普通用户和真实安装包有证据；新增代码集中在平台文件访问和窄化协议接缝。

第二阶段目标：明确授权后，固定随包Python与PowerShell 7在所选真实项目执行，兼容技能使用固定版本资源，保持原业务运行和真实取消/恢复。先贯通Python及兼容技能，再接入PowerShell；两者均属于第二阶段，Python不等待PowerShell即可按证据先开放，整体结项仍包含两者。两阶段共用执行协议骨架，文件和脚本保持独立授权及能力门槛。

**Non-Goals:** Bash/cmd语法翻译、模型指定任意可执行文件的通用启动工具、任意包安装、GUI/Office/COM自动化、系统服务/驱动/管理员账号、网络盘/UNC/非NTFS/云占位透传、Win7/ARM64、跨端同步。阶段一不执行任何脚本、不改项目ACL，也不暴露通用delete/rename；阶段二脚本可在获准项目内产生删除等副作用。脚本可能派生进程，所有后代均须受同一访问边界和Job约束；本次不新增系统级可执行文件白名单产品，也不保证任意程序兼容。预览始终不执行项目代码。网络依赖经已有企业/服务器工具显式调用，本次脚本进程不直接获得网络或企业凭据。

## Decisions

### D1. 文件操作与脚本边界分开，有限helper承担文件动作

先交付ls/search_files/read/write/edit。Rust helper仅执行固定文件动作，不加载项目代码、不接受module/argv/shell，不派生普通worker。其可信原生实现属于受信计算基；威胁模型覆盖恶意路径/协议、目录链接替换、断网重投及迟到响应，不承诺抵御内核漏洞或有权改写安装包的本机管理员。

共享原生库抽出平台路径/句柄模块，Cargo按cfg(unix)/cfg(windows)选择实现，libc仅unix依赖。原fs-guard(.exe)保留list/stat/read/search白名单；新增project-fs-guard.exe为独立binary入口，仅额外支持create/replace/edit的窄化私有操作。复用帧和根对象实现而不复制两份安全算法，测试固定两种入口各自白名单。代码共享不赋予只读入口写权限。

不采用Node/Python字符串检查后open，五文件工具不等待AppContainer。第二阶段通过独立隔离launcher运行不受信脚本，单靠Job Object或文件helper不能证明脚本隔离。本次两个阶段都不把bash翻译成PowerShell。

### D2. Windows路径按根句柄逐组件解析

原生选择后由宿主打开实际目录并记录卷标识/File ID、类型和授权版本；root handle及必要父链保持有效，路径只在本机保存。初次解析也检查目录对象和存储类型，不把来自页面的绝对路径转成授权。

原生后端使用用户态NtCreateFile的OBJECT_ATTRIBUTES.RootDirectory，每次仅解析一个相对组件；结合FILE_OPEN_REPARSE_POINT打开组件本身并读取属性，reparse point一律拒绝，不依赖单个标志或GetFinalPathNameByHandle的前缀判断。逐层持有句柄并采用阻止删除/重命名的共享策略；共享冲突返回可重试错误，不能退回可替换字符串路径。NtCreateFile用户态适配集中在小型Rust封装，并在目标OS版本做真实探针；公开接口不暴露NT对象名称。

拒绝绝对/盘符相对路径、UNC、设备命名空间、冒号/ADS、保留设备名、尾点/空格和越界组件。正常大小写和Unicode原样支持；8.3别名不用于授权标识，无法证明同一对象时拒绝。读写均拒绝不安全多硬链接目标，根及中间目录禁止reparse；在线云占位目录、网络/映射盘和不支持卷类型显示具体限制。卷根、用户home根、系统/安装/身份/日志控制目录不得作为项目根，合法项目子目录不因位于home下被误拒绝。

root路径重查发现对象改变即撤权；保持原handle保证已开始操作不被重定向。文件面板枚举/stat也走同一后端，不能先读越界元数据再过滤。搜索按现有schema执行有界目录遍历，不直接对不受控路径运行rg.exe。

### D3. 有限原地写入与可识别冲突

write新建要求目标不存在；覆盖及edit须携带读取时返回的expected_version。新版合同定义不透明版本，绑定项目/相对路径、对象身份及内容版本，不仅用mtime/size；模型与面板均传递该值，helper在写入前核对实际对象和内容。缺失/过期返回无写入的版本缺失/冲突，保留待提交内容，调用方重新读取、重建修改再提交，不自动强制覆盖或换命令ID重放旧意图。现有file-versions/command-runner是“发现变化仍执行再告警”，须新增写前阻断，不改变旧v2既有语义。按实际卷/File ID项目根串行副作用，不按别名加锁。

覆盖在锁定目标/父目录后检查版本，在同目录以CREATE_NEW建立随机暂存，写入并flush，重验目标对象和版本，再按句柄执行原子替换（NtSetInformationFile/FileRenameInformation等经探针验证的实现）。不对现有硬链接inode原地写入；临时文件/目录同样由已持有父句柄创建，禁止跨卷移动。原有权限不得被静默扩大，失败不能留下过宽ACL或半文件。新建拒绝意外覆盖；需要的新子目录由有限mkdir内部步骤在已授权根下创建，不向模型暴露通用rename/delete。

普通外部编辑器修改、目标替换、链接竞争和共享冲突均有探针。共享/锁策略与实际发布API的兼容性须先验证：无法锁定原目标或不能证明发布对象时失败，不声称对任意外部进程完全事务化。多文件操作不承诺原子事务，逐文件返回效果；部分完成不宣称全部回滚。用户原文件禁止作为清理目标，清理只根据本任务已记录的暂存对象身份执行，绝不通过dirname向上推算根目录。

发布原型还须验证权限保持：新建按获准父目录正常继承；覆盖保持目标DACL访问语义、owner及适用完整性标签，不能将目录较宽权限带入较窄目标。仅在私有暂存对象配置发布所需元数据，不为取得文件能力给项目根或既有文件放宽ACL，不提权补齐。普通文件的只读/隐藏/系统属性保持，修改时间反映新内容；不承诺File ID、创建时间或全部元数据不变。含额外数据流、EFS或无法安全保留属性的目标先明确拒绝覆盖；发布前不能保存必要访问语义或发现安全描述符改变则失败，发布后不明仍标未知。W05/W06记录准确支持范围，不把原子rename当作权限保持证明。

### D4. 新版统一执行合同先定稿，旧v2保持冻结

任务3.1是业务接入硬前置：采用同一project_execution的新主版本，计划交付contracts/desktop/v3.json、Python/TS校验与类型、双方协商和兼容测试。旧contracts/desktop/v2.json的win32.supported=false及POSIX/Bash语义保持；test_desktop_execution_contract.cjs:282/286是当前拒绝行为的证据，不以改旧断言/平台标志开启Windows。原生文件/隔离原型可先探索，但网关、授权、模型、面板接入均须在新版合同定稿后进行。

两阶段共用请求、开始许可、结果、取消、状态、WSS与持久队列，不另建project_files/project_scripts两套顶层协议。第一阶段即定义可选脚本工具与运行时的扩展规则，未实现工具不发布为可用。合同是版本、工具名/schema、运行时及协商语义的唯一来源；平台验收只作为可用性的一个输入，不能再维护独立Windows工具名单。

新脚本工具暂名run_script，定稿时核对模型工具注册、配置/MCP命名和内部接口；目前同名script_executor.run_script是内部函数，不作为已发生工具注册冲突的证据。最终名称在合同唯一登记，生成类型/工具描述/调用方一致；不沿用bash名称而改变语义。文件schema包含expected_version、新建/覆盖前置、明确冲突与效果结果；禁止调用者指定argv/env/module/root_path或服务器地址。

复用现有files/scripts面及desktop_project_execution_enabled、desktop_project_scripts_enabled，第一阶段scripts_verified=false。有效能力取部署策略、平台证据、双方版本/工具/运行时协商、本机后端、当前授权的交集；已有POSIX accepted或开关开启不授予Windows能力。默认不加Windows专属开关，确有独立灰度/应急关闭需求时记录理由、默认关闭且只收紧交集。新脚本启动要求文件面可用；在途脚本对helper故障的依赖按D6判断。未知版本/旧端/无能力均明确拒绝，不回落服务器同名目录或普通进程。

沿用既有身份/原生purpose及binding/grant记录。operations必须由明确确认生成，绑定服务器/用户/租户/设备/项目及grant版本；文件授权仅五工具，脚本额外确认其operation及运行时。operations集合摘要、grant版本和运行时约束纳入开始许可及参数摘要，客户端与服务端在入队/开始时核对，不能信任设备hello自报权限。持久字段缺省无新权限，旧grant不回填升级；旧v2记录保持原解释。缺字段、改集合、过期grant、越运行时请求均拒绝，重启不恢复活动授权。任务3.3/6.2验证迁移、回退及跨版本读取，不增加第二份身份真值。

### D5. 复用派发、journal与来源，不复制业务真值

模型保持原业务运行，工具schema/描述明确Windows本机来源，远程模式模型循环仍在服务器。本机模式复用已登记后端/设备网关，禁止为文件操作再开Agent循环。read-only输入可经现有只读路由读取，绝不进入write/edit；已选读写项目的五工具必须经统一执行派发路由到有限helper，失败明确返回，不再扫描服务器同名目录。

复用command-runner的同根串行和journal，沿用命令ID、工具/参数摘要及身份作用域去重；合同升级时验证版本差异不造成同ID重复执行或旧结果误用，仅补充确实缺少的字段。写入开始意图先持久化，完成回执记录发布对象/版本；崩溃发生在发布与回执间标outcome_unknown，不自动重跑。旧记录只读兼容且不升级授权，不复制第二份数据库。重复开始许可原子消费，结果回传重验连接/身份，撤销后已发生副作用仍按原作用域记录。

面板/搜索/模型/产物统一来源adapter；只有helper核实文件存在/版本后发布产物。引用保存设备、项目、相对路径、版本；原根不上传。网络上传走原显式传输，必要工具结果可进入模型。本机路径文本即使作为用户明确请求的输出，也不能被当作服务器定位或授权依据。

### D6. 构建、运行时选择与故障依赖

release.yml的Windows分支构建两个helper，Windows打包配置显式注入并签名，固定架构/依赖/摘要，开发态和包内候选采用实际.exe名称。accessSync不自动补后缀，不能由spawn行为推断候选可找到；真包验证缺失/隔离/版本错误。正式安装不从PATH或任意env路径加载未知helper，代码签名不作为隔离证明。普通用户不需系统Rust/Python或管理员权限，文件工具不为获取能力改写项目ACL，覆盖权限保持按D3执行。

第二阶段比较扩展冻结worker后复用与最小随包CPython的隔离资源范围、依赖适配、包体及维护成本，择优并记录。现有冻结worker不是通用脚本入口，复用须计入新增入口及后端资源收敛的改造成本，不重复验证其能否直接执行任意脚本。复用是偏好，不要求穷尽不可行证明。固定PowerShell 7随后接入，两种语言分别按证据开放。

文件helper失效时停止该作用域新文件操作；按产品前置同时拒绝新脚本、撤销未消费开始许可。在途脚本不因单纯文件服务故障自动终止：宿主能独立确认原脚本grant、根对象、隔离及Job均有效时可继续到真实终态；若实际依赖helper维持这些条件、授权撤销、根改变或无法确认隔离，则必须取消并确认全树停止。脚本运行许可与文件helper通道分别管理，不能宣称撤销脚本grant却继续执行。

helper恢复后文件重新选择/确认，不复活旧grant。在途脚本产物不能经可信文件路径验证时标待核验，不伪报可打开；文件操作及脚本效果不明时不重跑。停止/清理未确认则报告未完成，聊天和其他独立作用域保留。原生/真包/两模式证据齐备后才开放对应Windows平台能力。

### D7. 第二阶段脚本语义与兼容技能

启用D4定稿的新版脚本工具（暂名run_script）的python/powershell运行时，复用第一阶段执行链路和原ExecutionRun。输入为脚本文本或获权固定skill入口及数据参数；可执行路径由本地受信launcher解析，模型不能指定executable/env/module或覆写cwd。最终名称/schema、错误及版本兼容由合同统一提供，旧v2 Windows/Bash拒绝保持，只向协商了工具和运行时的端发送命令。

先用经验证的随包Python运行.py并贯通首批Excel/文档合成Skill，再以固定PowerShell 7运行.ps1；每种语言通过自身原型、隔离及真包证据后分别声明可用，未就绪语言明确拒绝，不阻塞已验收Python。使用无用户profile、受控启动参数及私有临时脚本文件，不把POSIX字符串转义后当PowerShell，不调用系统PATH上的python/pwsh/cmd。Skill资源按服务器/租户/用户和版本隔离，完整包校验后只读提供，业务cwd是已授权项目；.sh、依赖未支持GUI/网络/运行库的Skill明确不兼容，不改写源脚本掩盖差异。目标案例验证真实处理结果，不承诺所有历史Skill自动兼容。子进程允许性取决于同一OS边界、资源资格和Job约束；不将拒绝模型指定executable误写成禁止所有子进程。

文件grant不升级。用户额外确认项目脚本权限及删除/覆盖等可能效果，服务端记录operations/run-script/runtime/版本/作用域，本机也重验；每次调用继续按现有审批/配额/审计规则处理。复用现有脚本开关和能力面，以Windows隔离及逐运行时证据决定可用性，文件面开关不能放开脚本。脚本可先声明准备中，第一阶段正常可用；不能以第二阶段仍关闭或PowerShell未完成宣称整个change完成。

首批清单列出具体Excel/文档Skill版本、入口、Python包/系统库、模板和输出验证方法；声明这些离线案例及基本Python/PowerShell脚本可用，不由此推导Git、Node、包管理器、任意构建/测试链或所有历史Skill可用。新增程序/网络依赖另行验证，缺依赖明确拒绝。

### D8. 先验证最小脚本隔离原型，再确定身份生命周期

LPAC（Less Privileged AppContainer）为首选访问边界候选，Job Object负责进程树和资源。先用普通用户、真实项目及随包Python完成最小原型：正常读写、根外/凭据/网络拒绝、一个派生进程、取消及异常恢复；随后补PowerShell原型。实际token/能力、文件访问和子进程必须经目标OS探针验证，不能凭API创建成功定案。接业务前形成简短决策记录，包含运行时复用结论、系统例外、身份作用域及恢复方式；未经证实不把候选架构写成已可交付实现。

身份生命周期比较每次运行独立身份与同一服务器/租户/用户/设备/项目授权会话身份。只有会话中各次运行实际允许资源一致、不会合并其他项目或不同Skill的额外权限、撤权能停止全部相关进程时，才可选会话复用；否则采用每次运行身份。同一时刻不同授权范围不能共用会累加访问权的身份。每次运行仍有独立Job、临时资源和审计记录。选择满足边界且资源改动/恢复成本较小的方案，不强制每条命令创建SID或修改ACL。

会话身份复用沿用同根副作用串行，下一运行开始前须确认上一Job及其后代全部退出、本运行临时资源和仅本运行有效的授权已清理，保留的会话级资源资格仍一致。独立Job本身不证明临时区或进程互访已隔离；S05/S07须验证连续运行、取消后派生进程滞留和临时授权清理失败。任何一项未确认即阻止该身份/项目下一运行，不通过新命令、重连或换Job绕过；其他独立作用域不受连带阻断。

不授予internet/intranet/loopback、COM、设备或桌面自动化能力；只放行必要系统/运行时只读资源、当前Skill只读资源，以及当前项目与本次临时区的约定读写。系统运行时必要例外逐项记录，不宣称任何根外元数据都不可见；身份库、Bearer、其他项目/用户缓存必须不可读写。

强制保护的是宿主身份库、控制面令牌、未授予脚本的企业凭据及其他作用域数据。授权项目内的.env、证书和业务配置属于获准内容，不能同时承诺完整项目访问和自动隐藏其中所有秘密；原生确认及说明明确项目内容可能被读取并进入模型。本次不承诺通用秘密识别/脱敏，不以Skill包过滤代替真实项目隔离。宿主控制面数据不因位于项目内而获准，发现根与其重叠则拒绝授权并提示更换目录。

直接项目执行可能需要给隔离身份增加项目/运行时访问ACE，须在脚本授权中明示；不偷偷修改ACL、提权、降低完整性标签或放宽Everyone/ALL APPLICATION PACKAGES。保护继承不自动等于拒绝，按实际访问与能否安全配置/恢复判断。句柄遍历及链接拒绝只解决定位，不证明权限传播/恢复正确；不能满足边界时拒绝该配置，文件面保留。

权限生命周期原型（7.2）是正式ACL实现/脚本业务接入硬前置：覆盖显式/继承/受保护、NULL/空DACL、硬链接、用户并发改ACL、新增对象、外部移动对象及崩溃重启。继承ACE会传播到子对象，删除它不一定恢复原访问语义；硬链接共享底层安全描述符。选择可精确跟踪、恢复的最小授权集合，不能证明安全恢复的组合在改动前拒绝，不做未经验证的递归传播。原型须给出对象身份定位、变更覆盖、移出根后恢复策略及大目录成本；仅扫描原目录或记录初始路径不算完成。若正常目录变动下不能维持边界，修订脚本后端方案，不靠用户承诺“不移动文件”宣称隔离。

实际修改ACL前持久记录SID、作用域、对象身份、原安全描述符必要状态及所加ACE。恢复核对对象和当前状态，移除本次权限并验证未意外改变原访问语义，不盲目覆盖旧DACL或丢失用户并发修改。只删ACE或容器profile不代表已恢复；对象失联、状态冲突或覆盖不明时报告具体残留/恢复未完成、保留记录，不自动清理用户文件。停止相关进程，禁止复用残留SID并阻止受影响身份/项目新脚本，不换身份绕过。区分可自动确认恢复与需要人工处理的异常，不承诺所有异常自动恢复。每次运行身份在结束回收，会话身份在撤权/退出/会话结束先停全部Job再回收，重启不复活授权。原型失败须修订设计，不降级普通进程或仅用Job。

launcher在脚本可执行前创建受限token和完整环境/继承句柄白名单，将挂起进程纳入本次Job后再恢复，禁breakaway，配置kill-on-job-close、执行时间/进程数/内存上限。进程树退出确认后才能报告取消；嵌套Job/分配失败时不恢复线程。输出有界，超限和非零退出保留真实原因。Job只管理资源/生命周期，LPAC才承担访问边界，二者不可替代。

恶意脚本可以使用原生文件API、动态路径、硬链接/reparse和派生进程，仍必须不能获得其他根、控制面、网络或凭据；探针覆盖COM/子进程/进程句柄和继承资源。具备宿主权限且主动修改ACL/安装产物的恶意本机进程不在隔离防御承诺内，但正常外部编辑与目录变更不能造成静默错误项目写入。脚本的部分修改/删除不承诺回滚；发布前崩溃或回执丢失继续标记未知效果，不自动重跑。

### D9. 两阶段发布与整体完成

阶段一以W01–W16为门槛，单独发布文件能力；阶段二以S01–S12为门槛，先Python/兼容技能，后PowerShell。验收按acceptance.md的OS、连接模式、运行时分层分配，不要求全部用例做三者笛卡尔积。不追加未指明编号的原A系列用例；已有通用证据可明确映射到W/S项复用，发现缺口则先更新具体用例。开始第二阶段原型不必等待第一阶段正式发版，但接入业务必须满足其grant/文件边界接缝。整个change只有两阶段及两种语言的适用任务/证据完成才能归档，脚本失败或关闭不撤销已验收文件能力。

## Risks / Trade-offs

- [NT API封装和共享模式兼容] → 先做真实路径替换/原子发布探针，再接业务；不满足边界不得用字符串回退。SDK示例是API依据，不是已验收证据。
- [拒绝reparse影响OneDrive/网络项目] → 首期明确仅本地NTFS普通目录，显示限制；扩展存储类型另验，不静默下载/穿透。
- [执行合同升级的兼容成本] → 新版合同先定稿，旧v2冻结；两阶段共用执行骨架和默认开关，新增平台控制须有独立灰度/应急理由且不能扩大权限。
- [读写授权仍可能造成用户项目改动] → 明确目录/用途及版本冲突，原有动作审批照常；安全边界不等同自动备份或代码正确性保证。
- [第二阶段LPAC/运行时兼容与权限清理] → 先验证运行时、边界和ACL恢复，失败不降级、不误报通过；第一阶段可独立发布，第二阶段仍属本change必需交付，不承诺所有技能兼容。

## Migration Plan

1. 先完成helper探针及统一合同兼容合成测试，不改生产配置/用户ACL或业务数据；不修改旧change的任务完成状态。
2. 部署服务端兼容协商支持，再安装新客户端；任一端缺少新能力协商均不开放Windows读写，原聊天及已获准只读行为保持。
3. 目标包及Win10/Win11证据齐备后开放已有文件切片的Windows平台状态，使用现有部署开关；逐台/逐会话原生选择，旧候选不升级。
4. 回退关闭新任务并保留journal、回执、原文件和历史引用；等待进行中发布确定终态后停helper，未知结果提示人工核对，不删文件或自动重跑。旧版不理解的新记录保留，不强制降级改写。
5. 第二阶段保持Windows脚本未验收，先完成隔离原型/决策，再按运行时验证S01–S12及真实包；通过的语言可依现有脚本开关开放，另一语言仍拒绝。现有文件grant须额外脚本确认。脚本回退先停止进程树并清理隔离身份授权，保留未知效果和文件数据，不能只关闭UI留下后台进程。

## 前置决策与未决实施参数

- D4新版合同定稿、D3文件发布/权限保持、D8最小隔离与权限生命周期均为明确前置任务；失败须修订方案再接业务，不属于可推迟到发布后的参数。当前未证明第二阶段完整可行。
- 原型决策比较运行时和隔离身份生命周期；PowerShell随后接入，两种语言均须各自验收。
- 写入版本hash的增量缓存、helper错误码与现有HTTP映射可在实施中决定，必须满足本规范的对象/版本/未知效果边界。
- 签名服务与Rust工具链精确版本复用当前构建配置，落证据时固定产物摘要；不以开发者机器构建成功替代安装包验收。

## Sources and seam compatibility

- NtCreateFile的RootDirectory相对打开及reparse选项：[Microsoft Win32文档](https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntcreatefile)。实施仍需逐组件和实际对象检查，不能只设置一个标志。
- 第二阶段API依据：[AppContainer/LPAC](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer)、[Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)。这些资料不等于本项目运行时兼容或安全探针已通过。
- 冲突基线共享接缝：`agent/memory/conversation_store.py`（seam:conversation-store）保持原会话唯一归属；`agent/tools/scheduler/integration.py`、`tests/test_scheduler_web_update.py`（seam:scheduler）保持现有调度接口及回归，本次不直接修改，scheduler不从交互目录grant获取文件操作资格。

- 权限恢复依据：[硬链接共享安全描述符](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-createhardlinkw)、[继承ACE传播及NULL/空DACL差异](https://learn.microsoft.com/en-us/windows/win32/secauthz/automatic-propagation-of-inheritable-aces)。API依据不替代D8原型证据。
