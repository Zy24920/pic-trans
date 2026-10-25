# -*- coding: utf-8 -*-

# This code shows an example of text translation from English to Simplified-Chinese.
# This code runs on Python 2.7.x and Python 3.x.
# You may install `requests` to run this code: pip install requests
# Please refer to `https://api.fanyi.baidu.com/doc/21` for complete api document

import requests
import random
# import json
from hashlib import md5

# Set your own appid/appkey.
appid = ''
appkey = ''

# For list of language codes, please refer to `https://api.fanyi.baidu.com/doc/21`
from_lang = 'zh'
to_lang =  'en'

endpoint = 'http://api.fanyi.baidu.com'
path = '/api/trans/vip/translate'
url = endpoint + path

query = '帮我生成一些歌曲推荐，请用中文回答'

# Generate salt and sign
def make_md5(s: str, encoding: str='utf-8') -> str:
    return md5(s.encode(encoding)).hexdigest()

def run(querys: str, from_lang: str=from_lang, to_lang: str=to_lang) -> str: #from_lang 可设置为 auto 来自动识别语言
    salt = random.randint(32768, 65536)
    sign = make_md5(appid + querys + str(salt) + appkey)
    # Build request
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    payload = {'appid': appid, 'q': querys, 'from': from_lang, 'to': to_lang, 'salt': salt, 'sign': sign}
    # Send request
    try:
        response = requests.post(url, params=payload, headers=headers, timeout=30)
        response.raise_for_status()
        result = response.json()
        
    except requests.RequestException as exc:
        raise RuntimeError(f"百度翻译网络请求失败：{exc}") from exc

    except ValueError as exc:
        raise RuntimeError(f"百度翻译返回的不是有效 JSON：{exc}") from exc

    if "error_code" in result:
        raise RuntimeError("百度翻译接口错误："f"{result.get('error_code')} - "f"{result.get('error_msg', '未知错误')}")
            
    try:
        # Show response
        # data = json.dumps(result, indent=4, ensure_ascii=False)
        # q 中用换行符分隔的多段文本会各自返回一条翻译结果，需全部拼接
        return '\n'.join(item['dst'] for item in result['trans_result'])

    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"百度翻译返回数据格式异常：{result}") from exc

def main():
    # query = "帮我生成一些歌曲推荐，请用中文回答"
    try:
        result = run(query, from_lang="auto", to_lang="en",)
        # print("翻译结果：")
        print(result)

    except Exception as exc:
        print(f"翻译失败：{exc}")
        
if __name__ == '__main__':
    main()
