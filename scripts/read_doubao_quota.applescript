-- 从已打开的豆包额度管理页刷新并返回最小可见文本。
-- 只重载匹配的额度页；不新开页面、不点击，也不读取 Cookie、localStorage 或浏览器配置。

on run argv
  if (count of argv) is 0 then error "缺少豆包额度页 URL" number 1000
  set targetUrl to item 1 of argv
  set refreshPage to ((count of argv) > 1 and item 2 of argv is "refresh")
  set jsCode to "(() => { const lines = (document.body ? document.body.innerText : '').split(String.fromCharCode(10)).map(line => line.trim()).filter(Boolean); const current = lines.findIndex(line => line === '当前时段'); const weekly = lines.findIndex((line, index) => index > current && /^近\\s*7\\s*天$/.test(line)); if (current < 0 || weekly < 0) return ''; const record = lines.findIndex((line, index) => index > weekly && line === '订阅记录'); let management = -1; for (let index = 0; index < current; index += 1) { if (lines[index].includes('订阅与额度管理')) management = index; } const candidate = management > 0 ? lines[management - 1] : ''; const plan = candidate.endsWith('套餐') && !candidate.startsWith('升级至') && !candidate.startsWith('购买') ? candidate : ''; const managementLine = management >= 0 ? lines[management] : ''; return [plan, managementLine, ...lines.slice(current, record > weekly ? record + 1 : weekly + 4)].filter(Boolean).join(String.fromCharCode(10)); })()"

  tell application "Google Chrome"
    repeat with chromeWindow in windows
      repeat with chromeTab in tabs of chromeWindow
        set tabUrl to ""
        try
          set tabUrl to URL of chromeTab as text
        end try
        if tabUrl starts with targetUrl then
          if refreshPage then
            reload chromeTab
            delay 0.5
            repeat with attempt from 1 to 80
              if (loading of chromeTab) is false then exit repeat
              if attempt is 80 then error "豆包额度页刷新后未加载" number 1002
              delay 0.25
            end repeat
          end if

          repeat with attempt from 1 to 60
            set quotaText to execute chromeTab javascript jsCode
            if quotaText is not missing value then
              set quotaString to quotaText as text
              if quotaString is not "" then return quotaString
            end if
            delay 0.25
          end repeat
          error "豆包额度页刷新后未加载" number 1002
        end if
      end repeat
    end repeat
  end tell

  error "未找到已打开的豆包额度管理页" number 1001
end run
