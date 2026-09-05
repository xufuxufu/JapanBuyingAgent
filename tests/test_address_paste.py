from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


def _run_parser(cases: list[str]) -> list[dict | None]:
    node = shutil.which("node")
    assert node, "地址粘贴解析测试需要 Node.js"
    root = Path(__file__).resolve().parents[1]
    module_path = json.dumps(str(root / "app" / "static" / "address_paste.js"))
    cases_json = json.dumps(cases)
    program = f"""
const {{ parseAddressPasteText }} = require({module_path});
const cases = {cases_json};
console.log(JSON.stringify(cases.map((text) => parseAddressPasteText(text))));
"""
    completed = subprocess.run(
        [node, "-e", program], cwd=root, text=True, encoding="utf-8", capture_output=True, timeout=30, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_parses_space_separated_triplet():
    result, = _run_parser(["小徐 13900001111 湖北武汉市洪山区xxx"])
    assert result == {"name": "小徐", "phone": "13900001111", "address": "湖北武汉市洪山区xxx"}


def test_parses_comma_separated_triplet():
    result, = _run_parser(["小徐，13900001111，湖北武汉市洪山区xxx"])
    assert result == {"name": "小徐", "phone": "13900001111", "address": "湖北武汉市洪山区xxx"}


def test_parses_labeled_multiline_form():
    text = "收件人：小徐\n电话：13900001111\n地址：湖北武汉市洪山区xxx"
    result, = _run_parser([text])
    assert result == {"name": "小徐", "phone": "13900001111", "address": "湖北武汉市洪山区xxx"}


def test_parses_newline_separated_triplet():
    text = "小徐\n13900001111\n湖北武汉市洪山区xxx"
    result, = _run_parser([text])
    assert result == {"name": "小徐", "phone": "13900001111", "address": "湖北武汉市洪山区xxx"}


def test_no_phone_number_returns_null_and_never_guesses():
    result, = _run_parser(["湖北武汉市洪山区xxx"])
    assert result is None


def test_empty_text_returns_null():
    result, = _run_parser([""])
    assert result is None


def test_landline_or_short_numbers_are_not_mistaken_for_mobile_phone():
    # A 10-digit or non-1[3-9]-prefixed number must not be picked up as a phone
    # -- only real China mobile numbers should trigger a parse at all.
    result, = _run_parser(["小徐 02712345678 湖北武汉市洪山区xxx"])
    assert result is None


def test_labeled_form_with_different_label_wording():
    text = "姓名：李四\n手机：18600002222\n收货地址：上海市浦东新区xxx"
    result, = _run_parser([text])
    assert result == {"name": "李四", "phone": "18600002222", "address": "上海市浦东新区xxx"}


def test_address_first_then_name_colon_phone_on_its_own_line():
    # Regression: address on the first line, "name：phone" on the second --
    # the trailing "：" must not leak into the address.
    text = "湖北省武汉市洪山区测试大道88号测试花园C区\n张测试：13800000000"
    result, = _run_parser([text])
    assert result == {
        "name": "张测试", "phone": "13800000000",
        "address": "湖北省武汉市洪山区测试大道88号测试花园C区",
    }


def test_bare_name_fullwidth_colon_phone_no_address():
    result, = _run_parser(["张测试：13800000000"])
    assert result == {"name": "张测试", "phone": "13800000000", "address": ""}


def test_bare_name_halfwidth_colon_phone_no_address():
    result, = _run_parser(["张测试:13800000000"])
    assert result == {"name": "张测试", "phone": "13800000000", "address": ""}


def test_bare_name_space_phone_no_address():
    result, = _run_parser(["张测试 13800000000"])
    assert result == {"name": "张测试", "phone": "13800000000", "address": ""}


def test_bare_name_comma_phone_no_address():
    result, = _run_parser(["张测试，13800000000"])
    assert result == {"name": "张测试", "phone": "13800000000", "address": ""}


def test_name_colon_phone_then_address_on_next_line():
    text = "张测试：13800000000\n湖北省武汉市洪山区测试大道88号测试花园C区"
    result, = _run_parser([text])
    assert result == {
        "name": "张测试", "phone": "13800000000",
        "address": "湖北省武汉市洪山区测试大道88号测试花园C区",
    }
