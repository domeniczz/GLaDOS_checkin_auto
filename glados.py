import requests, json, os, re
from datetime import datetime

# 积分记录文件路径（同目录下），由 actions/cache 恢复 + git commit 兜底
POINTS_LOG_FILE = "points_log.json"
# 每个账号保留最近 N 条签到明细，避免文件无限增长
MAX_HISTORY = 10
# 当前剩余积分初始值（首次记录或字段缺失时使用）
# 可通过工作流环境变量 DEFAULT_CURRENT_POINTS 覆盖
DEFAULT_CURRENT_POINTS = int(os.environ.get("DEFAULT_CURRENT_POINTS", "281"))


def extract_points(message):
    """从 GLaDOS 签到返回的 message 中提取本次获得的积分。

    典型 message: "Checkin! Got 8 Points"
    重复签到或异常时通常无 "Got N Points" 字样，返回 0。
    """
    m = re.search(r'Got\s+(\d+)\s+Points', message or '')
    return int(m.group(1)) if m else 0


def load_points():
    """读取历史积分记录；文件不存在或解析失败时返回空字典。"""
    if not os.path.exists(POINTS_LOG_FILE):
        return {}
    try:
        with open(POINTS_LOG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_points(data):
    """写入积分记录到本地文件。"""
    with open(POINTS_LOG_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def new_record():
    """返回一条空白的账号积分记录。"""
    return {
        'total_points': 0,
        'checkin_count': 0,
        'last_checkin_date': '',
        'last_points': 0,
        'left_days': '',
        'current_points': DEFAULT_CURRENT_POINTS,
        'recent': []
    }


# GLaDOS 签到 message 汉化映射表（小写匹配，避免大小写差异）
# 已知返回值：
#   "Checkin! Got N Points"                              — 签到成功获得 N 积分
#   "Checkin Repeats!"                                    — 当日重复签到
#   "Checkin Successfully!"                               — 签到成功（无积分字样）
#   "Today's observation logged. Return tomorrow..."     — 今日已签到，明天再来
# 未匹配到的 message 原样返回，便于后续补充
_MESSAGE_TRANSLATIONS = [
    (re.compile(r'got\s+(\d+)\s+points', re.IGNORECASE),
     lambda m: f'签到成功！获得 {m.group(1)} 积分'),
    (re.compile(r"today's\s+observation\s+logged", re.IGNORECASE),
     lambda m: '今日已签到，明天再来'),
    (re.compile(r'checkin\s+repeats', re.IGNORECASE),
     lambda m: '今日已签到，请勿重复'),
    (re.compile(r'checkin\s+successfully', re.IGNORECASE),
     lambda m: '签到成功'),
]


def translate_mess(mess):
    """将 GLaDOS 返回的英文 message 汉化为中文，未知 message 原样返回。"""
    if not mess:
        return mess
    for pat, fn in _MESSAGE_TRANSLATIONS:
        m = pat.search(mess)
        if m:
            return fn(m)
    return mess


# WxPusher 极简推送接口
WXPUSHER_URL = "https://wxpusher.zjiecode.com/api/send/message/simple-push"


def send_push(content, summary="", content_type=2):
    """通过 WxPusher 极简推送发送消息。

    使用环境变量 WXPUSHER_SPT 作为 simplePushToken，未配置时静默跳过。
    content_type: 1=文字 2=html(只发body内) 3=markdown
    """
    spt = os.environ.get("WXPUSHER_SPT", "")
    if not spt:
        return
    # summary 截断到 100 字以内，未提供时取 content 前 20 字
    if not summary:
        summary = content[:20]
    summary = summary[:100]
    payload = {
        "content": content,
        "summary": summary,
        "contentType": content_type,
        "spt": spt,
    }
    try:
        resp = requests.post(WXPUSHER_URL, json=payload, timeout=10)
        # WxPusher 成功返回 code=1000
        if resp.status_code != 200 or resp.json().get("code") != 1000:
            print("WxPusher推送失败: " + resp.text)
    except Exception as e:
        print("WxPusher推送异常: " + str(e))


# -------------------------------------------------------------------------------------------
# github workflows
# -------------------------------------------------------------------------------------------
if __name__ == '__main__':
# WxPusher SPT（simplePushToken）申请地址 https://wxpusher.zjiecode.com
# 由 send_push 函数内部读取环境变量 WXPUSHER_SPT 推送
# 推送内容
    sendContent = ''
# glados账号cookie 直接使用数组 如果使用环境变量需要字符串分割一下
    cookies = os.environ.get("GLADOS_COOKIE", "").split("&")
    if cookies[0] == "":
        print('未获取到COOKIE变量') 
        cookies = []
        exit(0)
    url= "https://glados.rocks/api/user/checkin"
    url2= "https://glados.rocks/api/user/status"
    referer = 'https://glados.rocks/console/checkin'
    origin = "https://glados.rocks"
    useragent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/102.0.0.0 Safari/537.36"
    payload={
        'token': 'glados.one'
    }

    # 读取历史积分记录（由 actions/cache 恢复或 git checkout 兜底）
    points_data = load_points()
    today = datetime.now().strftime('%Y-%m-%d')

    for cookie in cookies:
        checkin = requests.post(url,headers={'cookie': cookie ,'referer': referer,'origin':origin,'user-agent':useragent,'content-type':'application/json;charset=UTF-8'},data=json.dumps(payload))
        state =  requests.get(url2,headers={'cookie': cookie ,'referer': referer,'origin':origin,'user-agent':useragent})
    #--------------------------------------------------------------------------------------------------------#
        try:
            time = state.json()['data']['leftDays']
            time = time.split('.')[0]
            email = state.json()['data']['email']
        except (KeyError, ValueError, TypeError):
            # Cookie 完全失效，状态接口无法返回正常数据
            email = '未知账号'
            time = '?'
        if 'message' in checkin.text:
            checkin_json = checkin.json()
            mess_raw = checkin_json.get('message', '')
            # 汉化展示用文案，日志/推送/历史记录都用汉化版
            mess = translate_mess(mess_raw)
            record = points_data.get(email, new_record())
            # 兼容旧记录：缺少 current_points 字段时初始化为默认值
            if 'current_points' not in record:
                record['current_points'] = DEFAULT_CURRENT_POINTS
            # 从签到返回的 list 提取本次积分和当前余额，取不到则回退正则
            checkin_list = checkin_json.get('list', [])
            if checkin_list:
                try:
                    gained = int(float(checkin_list[0].get('change', 0)))
                except (ValueError, TypeError):
                    gained = extract_points(mess_raw)
                try:
                    record['current_points'] = int(float(checkin_list[0].get('balance', record['current_points'])))
                except (ValueError, TypeError):
                    pass
            else:
                gained = extract_points(mess_raw)
            # 防止同一天重复运行被多算（重复签到时 gained 通常已为 0，此处再加一层日期兜底）
            if record.get('last_checkin_date') != today:
                record['total_points'] = record.get('total_points', 0) + gained
                record['checkin_count'] = record.get('checkin_count', 0) + 1
            record['last_checkin_date'] = today
            record['last_points'] = gained
            record['left_days'] = time
            recent = record.get('recent', [])
            recent.insert(0, {'date': today, 'points': gained, 'left_days': time, 'message': mess})
            if len(recent) > MAX_HISTORY:
                recent = recent[:MAX_HISTORY]
            record['recent'] = recent
            points_data[email] = record

            print(email+'----结果--'+mess+'----剩余('+time+')天----本次('+str(gained)+')----当前余额('+str(record['current_points'])+')')  # 日志输出
            sendContent += email+'----'+mess+'----剩余('+time+')天----本次('+str(gained)+')----当前余额('+str(record['current_points'])+')\n'
        else:
            send_push('<p>' + email + ' Cookie已失效，请尽快更新</p>', 'GLaDOS Cookie失效')
            print('cookie已失效')  # 日志输出
     #--------------------------------------------------------------------------------------------------------#   
    # 持久化积分记录（cache 命中时由 post-run 保存；同时由 workflow 末尾的 git commit 兜底）
    save_points(points_data)
    if sendContent:
        # 纯文本换行转 HTML <br>，加 H3 标题
        html_content = '<h3>GLaDOS 签到报告</h3>' + sendContent.replace('\n', '<br>')
        send_push(html_content, 'GLaDOS 签到报告')
