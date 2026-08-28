<#
  outlook_probe.ps1 —— 本地 Outlook 能力探测（只读）

  为什么需要它：
    「本地 Outlook」场景靠 COM 跟桌面版 Outlook 说话。COM 的行为在不同机器上差别很大
    ——缓存模式还是在线模式、Restrict 过滤能不能用、SenderEmailAddress 会不会触发
    「程序化访问」安全提示——这些只能在真机上实测，猜不出来。这个脚本就是去实测。

  它做什么：
    · 连接已登录的 Outlook，遍历文件夹，统计条数
    · 检查我们打算用的每个字段「有没有值、什么类型、多长」
    · 实测 Restrict 日期过滤是否可用、要多久

  它不做什么（重要）：
    · 不打印任何主题、正文、姓名、邮箱地址、附件文件名
    · 不发送、不回复、不删除、不修改任何邮件
    · 不联网、不写文件、不改任何设置

  输出被刻意设计成「可以安全粘贴给别人看」：只有字段是否存在、类型、长度、计数、耗时。
  唯一的例外是文件夹名称——你自己建的文件夹名可能有含义，扫一眼再贴。

  用法（在你自己的电脑上，普通 PowerShell 窗口即可，不需要管理员）：
      powershell -ExecutionPolicy Bypass -File outlook_probe.ps1

  如果弹出「某个程序正试图访问 Outlook 中存储的电子邮件地址信息」——那就是我们要找的
  那个安全提示。请点「拒绝」，然后把这件事告诉我：说明这台机器上取发件人地址受限，
  我会改用不触发提示的取法。
#>

$ErrorActionPreference = 'Continue'

function Line($s) { Write-Host $s }
function Head($s) { Write-Host ""; Write-Host "=== $s ===" }

Line "outlook_probe · read-only · $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
Line "PowerShell $($PSVersionTable.PSVersion) · 64bit=$([Environment]::Is64BitProcess)"

# ---------------------------------------------------------------- 预检
# 为什么要先查注册表再碰 COM：如果 profile 是「半成品」（账户登记了但消息存储从没建成），
# New-Object -ComObject Outlook.Application 会弹一个**模态**错误框
# （"The information store could not be opened"），脚本就卡在那儿不动，
# 而且用户桌面上莫名多一个对话框。先查一下就能提前给出人话诊断。
#
# 判断标准：profile 子树里有没有任何 .ost / .pst 路径。有 = 存储配过；没有 = 半成品。
# 注意这是 fail-open：查不出结论就照常往下走 COM，宁可弹框也不要误拦一个能用的 Outlook。
Head "preflight (registry, no COM yet)"
$storeConfigured = $null   # $true / $false / $null(未知)
try {
    $olKey = 'HKCU:\SOFTWARE\Microsoft\Office\16.0\Outlook'
    $defProf = $null
    if (Test-Path $olKey) { $defProf = (Get-ItemProperty $olKey -ErrorAction SilentlyContinue).DefaultProfile }
    Line "default profile                  : $(if ($defProf) { $defProf } else { '(not set)' })"
    $profRoot = "$olKey\Profiles"
    if ($defProf) { $profRoot = "$olKey\Profiles\$defProf" }
    if (Test-Path $profRoot) {
        $paths = @()
        Get-ChildItem $profRoot -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
            $props = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue
            if ($null -ne $props) {
                foreach ($n in $props.PSObject.Properties.Name) {
                    $v = $props.$n
                    if ($v -is [byte[]]) {
                        $s = [System.Text.Encoding]::Unicode.GetString($v) -replace "`0", ''
                        if ($s -match '\.(ost|pst)') { $paths += $s }
                    }
                }
            }
        }
        $storeConfigured = ($paths.Count -gt 0)
        Line "store paths in profile           : $($paths.Count)"
        # 只打「存在/缺失」，不打路径本身（路径里带用户名和邮箱地址）
        foreach ($p in ($paths | Select-Object -Unique)) {
            Line ("  ext={0} onDisk={1}" -f ([IO.Path]::GetExtension($p)), (Test-Path $p))
        }
    } else {
        Line "profile key                      : not found (unknown)"
    }
} catch {
    Line "preflight inconclusive           : $($_.Exception.Message)"
}

if ($storeConfigured -eq $false) {
    Line ""
    Line "诊断：这个 Outlook profile 有账户条目，但**没有任何消息存储**。"
    Line "     打开 Outlook 会报 'The information store could not be opened'。"
    Line "     profile 是半途创建失败的，不是网络或权限问题。"
    Line ""
    Line "修复（需要你手动登录，脚本不代做）：先完全退出 Outlook，然后运行"
    Line "     outlook.exe /manageprofiles"
    Line "     -> 新建一个 profile（别修旧的），填邮箱、登录、设为默认。"
    Line ""
    Line "已跳过 COM 探测，免得弹出模态错误框卡住。"
    exit 2
}

# ---------------------------------------------------------------- 连接
Head "connect"
$running = $null -ne (Get-Process OUTLOOK -ErrorAction SilentlyContinue)
Line "OUTLOOK.EXE running before probe : $running"
try {
    $ol = New-Object -ComObject Outlook.Application
    $ns = $ol.GetNamespace("MAPI")
    Line "COM attach                       : OK"
    Line "Outlook version                  : $($ol.Version)"
} catch {
    Line "COM attach                       : FAILED"
    Line "reason                           : $($_.Exception.Message)"
    Line ""
    Line "到这里就断了。先确认 Outlook 能正常打开邮箱，再重跑这个脚本。"
    exit 1
}

# ---------------------------------------------------------------- 存储
# 关心的是「缓存模式还是在线模式」：在线模式下每次取数都要往服务器跑一趟，
# 遍历上千封会慢到没法用，那就得改成只取最近 N 天 + 服务端过滤。
Head "stores"
try {
    Line "store count                      : $($ns.Stores.Count)"
    for ($i = 1; $i -le $ns.Stores.Count; $i++) {
        $st = $ns.Stores.Item($i)
        # ExchangeStoreType: 0=PrimaryExchange 1=DeliveryStore 2=PublicFolders 3=NotExchange
        # IsCachedExchange: $true = 有本地 .ost 缓存（快）
        Line ("  store[{0}] exchangeType={1} cached={2} dataFile={3}" -f `
              $i, $st.ExchangeStoreType, $st.IsCachedExchange, $st.IsDataFileStore)
    }
} catch { Line "stores probe failed              : $($_.Exception.Message)" }

try { Line "accounts in profile              : $($ns.Accounts.Count)" } catch {}

# ---------------------------------------------------------------- 文件夹树
# 只打两层。真实邮箱里文件夹能有上百个，全打出来没用还容易带出信息。
Head "folder tree (2 levels, names + counts)"
function Show-Folder($f, $indent) {
    try {
        Line ("{0}{1}  items={2} unread={3}" -f $indent, $f.Name, $f.Items.Count, $f.UnReadItemCount)
    } catch {
        Line ("{0}{1}  <count unavailable>" -f $indent, $f.Name)
    }
}
try {
    $root = $ns.Folders.Item(1)
    Line "root store                       : (name hidden)"
    for ($i = 1; $i -le [Math]::Min($root.Folders.Count, 25); $i++) {
        $f = $root.Folders.Item($i)
        Show-Folder $f "  "
        try {
            for ($j = 1; $j -le [Math]::Min($f.Folders.Count, 6); $j++) {
                Show-Folder $f.Folders.Item($j) "      "
            }
            if ($f.Folders.Count -gt 6) { Line "      ... +$($f.Folders.Count - 6) more" }
        } catch {}
    }
    if ($root.Folders.Count -gt 25) { Line "  ... +$($root.Folders.Count - 25) more top-level" }
} catch { Line "folder walk failed               : $($_.Exception.Message)" }

# ---------------------------------------------------------------- 默认文件夹
Head "default folders"
# 6=Inbox 5=SentMail 16=Drafts 3=DeletedItems 4=Outbox
foreach ($pair in @(@(6,'Inbox'), @(5,'SentMail'), @(16,'Drafts'), @(3,'DeletedItems'))) {
    try {
        $f = $ns.GetDefaultFolder($pair[0])
        Line ("  {0,-13} items={1} unread={2}" -f $pair[1], $f.Items.Count, $f.UnReadItemCount)
    } catch { Line ("  {0,-13} UNAVAILABLE: {1}" -f $pair[1], $_.Exception.Message) }
}

# ---------------------------------------------------------------- Restrict 过滤
# 这是性能的关键。如果 Restrict 可用，取「最近 7 天」就不用遍历整个收件箱。
Head "restrict / sort support (perf-critical)"
$inbox = $null
try { $inbox = $ns.GetDefaultFolder(6) } catch {}
if ($null -ne $inbox) {
    $since = (Get-Date).AddDays(-7).ToString("yyyy-MM-dd") + "T00:00:00Z"
    # 用 DASL 而不是 Jet 语法：Jet 的日期格式跟着系统区域设置变，中文环境常年踩坑。
    $dasl = '@SQL="urn:schemas:httpmail:datereceived" >= ' + "'" + $since + "'"
    try {
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $sel = $inbox.Items.Restrict($dasl)
        $n = $sel.Count
        $sw.Stop()
        Line "  DASL restrict (last 7d)        : OK  matched=$n  elapsed=$($sw.ElapsedMilliseconds)ms"
    } catch {
        Line "  DASL restrict                  : FAILED: $($_.Exception.Message)"
    }
    try {
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $items = $inbox.Items
        $items.Sort("[ReceivedTime]", $true)
        $first = $items.GetFirst()
        $sw.Stop()
        Line "  Sort by ReceivedTime desc      : OK  elapsed=$($sw.ElapsedMilliseconds)ms"
    } catch {
        Line "  Sort by ReceivedTime desc      : FAILED: $($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------- 字段可用性
# 只报告「有没有、什么类型、多长」。绝不打印内容本身。
Head "field availability on newest 5 inbox items (no content printed)"
function Probe($item, $name) {
    try {
        $v = $item.$name
        if ($null -eq $v) { return "$name=null" }
        if ($v -is [string]) {
            if ($v.Length -eq 0) { return "$name=empty" }
            return "$name=str($($v.Length))"
        }
        if ($v -is [datetime]) { return "$name=date" }
        if ($v -is [bool])     { return "$name=$v" }
        return "$name=$($v.GetType().Name)"
    } catch { return "$name=ERR" }
}
if ($null -ne $inbox) {
    try {
        $items = $inbox.Items
        $items.Sort("[ReceivedTime]", $true)
        $it = $items.GetFirst()
        $k = 0
        while ($null -ne $it -and $k -lt 5) {
            $k++
            # 注意：$x = try {...} catch {...} 是 PowerShell 7 语法，Windows 自带的 5.1
            # 解析不过。全脚本一律用显式 try/catch 语句，别图省事。
            $cls = "?"
            try { $cls = $it.MessageClass } catch { $cls = "?" }
            Line "  item[$k] class=$cls"
            # 分两行打，免得单行太长在终端里折断难读
            $g1 = @('Subject','SenderName','SenderEmailAddress','ReceivedTime','To','CC') |
                  ForEach-Object { Probe $it $_ }
            $g2 = @('UnRead','Importance','FlagStatus','Categories','ConversationID',
                    'ConversationTopic','BodyFormat','Size') |
                  ForEach-Object { Probe $it $_ }
            Line "         $($g1 -join '  ')"
            Line "         $($g2 -join '  ')"
            # 正文只报长度，不报内容
            try {
                $b = $it.Body
                $bl = if ($null -eq $b) { "null" } else { "$($b.Length) chars" }
                $h = $null
                try { $h = $it.HTMLBody } catch { $h = $null }
                $hl = if ($null -eq $h) { "none" } else { "$($h.Length) chars" }
                Line "         Body=$bl  HTMLBody=$hl"
            } catch { Line "         Body=ERR: $($_.Exception.Message)" }
            # 附件：只报个数和扩展名，不报文件名
            try {
                $ac = $it.Attachments.Count
                $exts = @()
                for ($a = 1; $a -le [Math]::Min($ac, 5); $a++) {
                    $fn = $it.Attachments.Item($a).FileName
                    $exts += if ($fn -match '\.([A-Za-z0-9]{1,6})$') { $matches[1].ToLower() } else { "noext" }
                }
                Line "         Attachments=$ac  exts=[$($exts -join ',')]"
            } catch { Line "         Attachments=ERR" }
            $it = $items.GetNext()
        }
        if ($k -eq 0) { Line "  (inbox empty — nothing to probe)" }
    } catch { Line "  field probe failed: $($_.Exception.Message)" }
}

# ---------------------------------------------------------------- MessageClass 分布
# 收件箱里不只有普通邮件：会议邀请、已读回执、投递失败通知都混在里面，
# 分类逻辑要知道各占多少，否则「未回复邮件」里会塞满自动回执。
Head "MessageClass distribution (newest 200)"
if ($null -ne $inbox) {
    try {
        $items = $inbox.Items
        $items.Sort("[ReceivedTime]", $true)
        $it = $items.GetFirst()
        $tally = @{}
        $n = 0
        while ($null -ne $it -and $n -lt 200) {
            $n++
            $c = "ERR"
            try { $c = $it.MessageClass } catch { $c = "ERR" }
            if ($null -eq $c) { $c = "null" }
            if ($tally.ContainsKey($c)) { $tally[$c]++ } else { $tally[$c] = 1 }
            $it = $items.GetNext()
        }
        Line "  sampled: $n"
        $tally.GetEnumerator() | Sort-Object Value -Descending | ForEach-Object {
            Line ("    {0,-40} {1}" -f $_.Key, $_.Value)
        }
    } catch { Line "  tally failed: $($_.Exception.Message)" }
}

Head "done"
Line "以上不含任何邮件内容，可以直接粘贴。"
if (-not $running) {
    Line ""
    Line "注意：探测前 Outlook 没在运行，这个脚本把它启动了。可以正常关掉。"
}
