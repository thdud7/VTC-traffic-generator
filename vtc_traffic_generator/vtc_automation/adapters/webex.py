import asyncio
import glob
import html
import json
import os
import platform
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from vtc_automation.event_log import emit_event, utc_now_iso

try:
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
except ModuleNotFoundError:
    PlaywrightError = Exception
    PlaywrightTimeoutError = TimeoutError

try:
    from playwright._impl._errors import TargetClosedError
except ModuleNotFoundError:
    TargetClosedError = RuntimeError

from .browser import BrowserMeetingAdapter


class WebexAdapter(BrowserMeetingAdapter):
    service_name = "webex"

    DEFAULT_SELECTORS = {
        "cookie_accept": [
            'button:has-text("Accept all")',
            'button:has-text("Accept All")',
            'button:has-text("Accept")',
            'button:has-text("I accept")',
            'button:has-text("수락")',
            '[data-test*="cookie"] button:has-text("Accept")',
            '[data-test*="cookie"] button:has-text("수락")',
        ],
        "cookie_reject": [
            'button:has-text("Reject all")',
            'button:has-text("Reject All")',
            'button:has-text("Reject")',
            'button:has-text("Decline")',
            'button:has-text("거절")',
            '[data-test*="cookie"] button:has-text("Reject")',
            '[data-test*="cookie"] button:has-text("거절")',
        ],
        "cookie_close": [
            '[data-test*="cookie"] button[aria-label*="Close" i]',
            '[data-test*="cookie"] button[aria-label*="닫기" i]',
            '[aria-label*="Close cookie" i]',
            '[aria-label*="쿠키" i][aria-label*="닫기" i]',
            'button[aria-label="Close"]',
            'button[aria-label="닫기"]',
        ],
        "cookie_settings": [
            'button:has-text("Cookie settings")',
            'button:has-text("Manage cookie settings")',
            'button:has-text("쿠키 설정 관리")',
        ],
        "join_from_browser": [
            'button:has-text("Join from your browser")',
            'a:has-text("Join from your browser")',
            'button:has-text("Join from this browser")',
            'a:has-text("Join from this browser")',
            'button:has-text("Continue in browser")',
            'a:has-text("Continue in browser")',
            'button:has-text("Continue in this browser")',
            'a:has-text("Continue in this browser")',
            'button:has-text("Join from browser")',
            'a:has-text("Join from browser")',
            'button:has-text("Use web app")',
            'a:has-text("Use web app")',
            'button:has-text("Use browser")',
            'a:has-text("Use browser")',
            'button:has-text("Open in browser")',
            'a:has-text("Open in browser")',
            'button:has-text("Join meeting")',
            'a:has-text("Join meeting")',
            'button:has-text("Join Meeting")',
            'a:has-text("Join Meeting")',
            'button:has-text("Join")',
            'a:has-text("Join")',
            'button:has-text("Join using browser")',
            'a:has-text("Join using browser")',
            'text="Having trouble? Join from your browser"',
            'button:has-text("브라우저에서 참여")',
            'a:has-text("브라우저에서 참여")',
            'button:has-text("브라우저에서 참가")',
            'a:has-text("브라우저에서 참가")',
            'button:has-text("브라우저로 참여")',
            'a:has-text("브라우저로 참여")',
            'button:has-text("브라우저로 참가")',
            'a:has-text("브라우저로 참가")',
            'button:has-text("브라우저에서 계속")',
            'a:has-text("브라우저에서 계속")',
            'button:has-text("이 브라우저에서 참여")',
            'a:has-text("이 브라우저에서 참여")',
            'button:has-text("이 브라우저에서 참가")',
            'a:has-text("이 브라우저에서 참가")',
            'button:has-text("웹에서 참여")',
            'a:has-text("웹에서 참여")',
            'button:has-text("웹에서 참가")',
            'a:has-text("웹에서 참가")',
            'button:has-text("웹 앱 사용")',
            'a:has-text("웹 앱 사용")',
            'button:has-text("앱 없이 참여")',
            'a:has-text("앱 없이 참여")',
            'button:has-text("앱 없이 참가")',
            'a:has-text("앱 없이 참가")',
            '[data-test="join-from-browser"]',
            '[aria-label*="Join from your browser"]',
            '[aria-label*="Join from this browser"]',
            '[aria-label*="Join from browser"]',
            '[aria-label*="Join using browser"]',
            '[aria-label*="브라우저"]',
            '[aria-label*="웹"]',
        ],
        "browser_join": [
            'button:has-text("Join from your browser")',
            'a:has-text("Join from your browser")',
            'button:has-text("Join from this browser")',
            'a:has-text("Join from this browser")',
            'button:has-text("Join from browser")',
            'a:has-text("Join from browser")',
            '[data-test="join-from-browser"]',
            '[aria-label*="Join from your browser"]',
            '[aria-label*="Join from this browser"]',
            '[aria-label*="Join from browser"]',
        ],
        "continue_in_browser": [
            'button:has-text("Continue in browser")',
            'button:has-text("Continue in this browser")',
            'a:has-text("Continue in browser")',
            'a:has-text("Continue in this browser")',
            'button:has-text("브라우저에서 계속")',
            'a:has-text("브라우저에서 계속")',
            '[aria-label*="Continue in browser"]',
            '[aria-label*="Continue in this browser"]',
            '[aria-label*="브라우저에서 계속"]',
        ],
        "use_web_app": [
            'button:has-text("Use web app")',
            'a:has-text("Use web app")',
            'button:has-text("Use browser")',
            'a:has-text("Use browser")',
            'button:has-text("웹 앱 사용")',
            'a:has-text("웹 앱 사용")',
            '[aria-label*="Use web app"]',
            '[aria-label*="Use browser"]',
            '[aria-label*="웹 앱 사용"]',
        ],
        "join_from_this_browser": [
            'button:has-text("Join from this browser")',
            'a:has-text("Join from this browser")',
            'button:has-text("이 브라우저에서 참여")',
            'a:has-text("이 브라우저에서 참여")',
            'button:has-text("이 브라우저에서 참가")',
            'a:has-text("이 브라우저에서 참가")',
            '[aria-label*="Join from this browser"]',
            '[aria-label*="이 브라우저"]',
        ],
        "open_in_browser": [
            'button:has-text("Open in browser")',
            'a:has-text("Open in browser")',
            'button:has-text("브라우저로 참여")',
            'a:has-text("브라우저로 참여")',
            'button:has-text("브라우저로 참가")',
            'a:has-text("브라우저로 참가")',
            '[aria-label*="Open in browser"]',
            '[aria-label*="브라우저로"]',
        ],
        "cancel_open_app_prompt": [
            'button:has-text("Cancel")',
            'button:has-text("Not now")',
            'button:has-text("Not Now")',
            'button:has-text("No thanks")',
            'button:has-text("취소")',
            '[aria-label="Cancel"]',
            '[aria-label*="Not now"]',
            '[aria-label*="Not Now"]',
            '[aria-label*="취소"]',
        ],
        "download_page_indicator": [
            'text="Get ready to join"',
            'text="Open \\"Webex Installer.dmg\\" after it downloads"',
            'text="Open Webex Installer.dmg after it downloads"',
            'text="Webex Installer.dmg"',
            'text="Download Webex"',
            'text="Webex 앱 다운로드"',
            'text="Download the Webex app"',
            'text="Open Webex after it downloads"',
        ],
        "installer_download_indicator": [
            'text="Get ready to join"',
            'text="Open \\"Webex Installer.dmg\\" after it downloads"',
            'text="Open Webex Installer.dmg after it downloads"',
            'text="Webex Installer.dmg"',
            'text="Download Webex"',
            'text="Download the Webex app"',
            'text="Webex 앱 다운로드"',
        ],
        "problem_joining_from_browser": [
            'text="Problem joining from browser?"',
            'text="Problem joining from your browser?"',
            'text="Having trouble joining from browser?"',
            'text=/브라우저에서 참여하는 데 문제가 있/',
            'text=/브라우저에서 참가하는 데 문제가 있/',
        ],
        "try_again_browser_join": [
            'text="Try again"',
            'text="Retry"',
            'text="다시 시도"',
            ':text("Try again")',
            ':text("Retry")',
            ':text("다시 시도")',
            'button:has-text("Try again")',
            '[role="button"]:has-text("Try again")',
            '[role="link"]:has-text("Try again")',
            'div:has-text("Try again")',
            'span:has-text("Try again")',
            'button:has-text("Retry")',
            '[role="button"]:has-text("Retry")',
            '[role="link"]:has-text("Retry")',
            'div:has-text("Retry")',
            'span:has-text("Retry")',
            'button:has-text("Try again from browser")',
            'a:has-text("Try again from browser")',
            'button:has-text("Join from browser")',
            'a:has-text("Join from browser")',
            'button:has-text("Join from your browser")',
            'a:has-text("Join from your browser")',
            'button:has-text("Continue in browser")',
            'a:has-text("Continue in browser")',
            'button:has-text("브라우저에서 다시 시도")',
            'a:has-text("브라우저에서 다시 시도")',
            'button:has-text("다시 시도")',
            'a:has-text("다시 시도")',
            'button:has-text("브라우저에서 참여")',
            'a:has-text("브라우저에서 참여")',
            'button:has-text("이 브라우저에서 참여")',
            'a:has-text("이 브라우저에서 참여")',
        ],
        "got_it_button": [
            'text="Got it"',
            'text="확인"',
            'text="알겠습니다"',
            ':text("Got it")',
            ':text("확인")',
            ':text("알겠습니다")',
            'button:has-text("Got it")',
            '[role="button"]:has-text("Got it")',
            '[role="link"]:has-text("Got it")',
            'div:has-text("Got it")',
            'span:has-text("Got it")',
            'button:has-text("확인")',
            '[role="button"]:has-text("확인")',
            '[role="link"]:has-text("확인")',
            'div:has-text("확인")',
            'span:has-text("확인")',
            'button:has-text("알겠습니다")',
            '[role="button"]:has-text("알겠습니다")',
            '[role="link"]:has-text("알겠습니다")',
            'div:has-text("알겠습니다")',
            'span:has-text("알겠습니다")',
        ],
        "try_again_button": [
            'text="Try again"',
            'text="Retry"',
            'text="다시 시도"',
            ':text("Try again")',
            ':text("Retry")',
            ':text("다시 시도")',
            'button:has-text("Try again")',
            '[role="button"]:has-text("Try again")',
            '[role="link"]:has-text("Try again")',
            'div:has-text("Try again")',
            'span:has-text("Try again")',
            'button:has-text("Retry")',
            '[role="button"]:has-text("Retry")',
            '[role="link"]:has-text("Retry")',
            'div:has-text("Retry")',
            'span:has-text("Retry")',
            'button:has-text("다시 시도")',
            '[role="button"]:has-text("다시 시도")',
            '[role="link"]:has-text("다시 시도")',
            'div:has-text("다시 시도")',
            'span:has-text("다시 시도")',
        ],
        "join_on_mobile_indicator": [
            'text="Join on mobile"',
            'text="모바일에서 참여"',
            'text="모바일에서 참가"',
        ],
        "app_download_indicator": [
            'text="Webex Installer.dmg"',
            'text="Download"',
            'text="Download Webex"',
            'text="Webex 앱 다운로드"',
            'text="Download Webex app"',
        ],
        "join_as_guest": [
            'button:has-text("Join as a guest")',
            'button:has-text("Continue as guest")',
            'a:has-text("Join as a guest")',
            '[aria-label*="guest"]',
        ],
        "guest_join": [
            'button:has-text("Join as a guest")',
            'button:has-text("Continue as guest")',
            'a:has-text("Join as a guest")',
            '[aria-label*="guest"]',
        ],
        "display_name": [
            'input:not([type="hidden"])[name="displayName"]',
            'input:not([type="hidden"])[name*="display" i]',
            'input:not([type="hidden"])[name="name"]',
            'input:not([type="hidden"])[name*="name" i]',
            'input:not([type="hidden"])[id*="display" i]',
            'input:not([type="hidden"])[id*="name" i]',
            'input:not([type="hidden"])[aria-label*="name" i]',
            'input:not([type="hidden"])[placeholder*="name" i]',
            'input:not([type="hidden"])[placeholder*="Your name" i]',
            'input:not([type="hidden"])[placeholder*="Display name" i]',
            'input:not([type="hidden"])[aria-label*="이름" i]',
            'input:not([type="hidden"])[placeholder*="이름" i]',
            'input:not([type="hidden"])[aria-label*="참가자" i]',
            'input:not([type="hidden"])[placeholder*="참가자" i]',
            'input:not([type="hidden"])[aria-label*="이름을 입력" i]',
            'input:not([type="hidden"])[placeholder*="이름을 입력" i]',
            'mdc-input input:not([type="hidden"])',
            'mdc-input textarea',
            'input[type="text"]',
            "input:not([type])",
            "textarea",
            '[role="textbox"]',
            '[contenteditable="true"]',
        ],
        "name_input": [
            'input:not([type="hidden"])[name="displayName"]',
            'input:not([type="hidden"])[name*="display" i]',
            'input:not([type="hidden"])[name="name"]',
            'input:not([type="hidden"])[name*="name" i]',
            'input:not([type="hidden"])[id*="display" i]',
            'input:not([type="hidden"])[id*="name" i]',
            'input:not([type="hidden"])[aria-label*="name" i]',
            'input:not([type="hidden"])[placeholder*="name" i]',
            'input:not([type="hidden"])[placeholder*="Your name" i]',
            'input:not([type="hidden"])[placeholder*="Display name" i]',
            'input:not([type="hidden"])[aria-label*="이름" i]',
            'input:not([type="hidden"])[placeholder*="이름" i]',
            'input:not([type="hidden"])[aria-label*="참가자" i]',
            'input:not([type="hidden"])[placeholder*="참가자" i]',
            'input:not([type="hidden"])[aria-label*="이름을 입력" i]',
            'input:not([type="hidden"])[placeholder*="이름을 입력" i]',
            'mdc-input input:not([type="hidden"])',
            'mdc-input textarea',
            'input[type="text"]',
            "input:not([type])",
            "textarea",
            '[role="textbox"]',
            '[contenteditable="true"]',
        ],
        "email_input": [
            'input[type="email"]',
            'input[name="email"]',
            'input[aria-label*="email" i]',
            'input[placeholder*="email" i]',
        ],
        "password_input": [
            'input[type="password"]',
            'input[name="password"]',
            'input[aria-label*="password" i]',
            'input[placeholder*="password" i]',
        ],
        "continue_button": [
            'button:has-text("Continue")',
            'button:has-text("Continue as guest")',
            '[aria-label="Continue"]',
            '[aria-label*="Continue"]',
        ],
        "next_button": [
            'button:has-text("Next")',
            '[aria-label="Next"]',
            '[aria-label*="Next"]',
        ],
        "use_computer_audio": [
            'button:has-text("Use computer audio")',
            'button:has-text("Use computer for audio")',
            '[aria-label*="Use computer audio"]',
            '[aria-label*="Computer audio"]',
        ],
        "start_meeting_button": [
            'button:has-text("Start meeting")',
            'button:has-text("Start Meeting")',
            'button:has-text("미팅 시작")',
            'button:has-text("회의 시작")',
            '[role="button"]:has-text("Start meeting")',
            '[role="button"]:has-text("Start Meeting")',
            '[role="button"]:has-text("미팅 시작")',
            '[role="button"]:has-text("회의 시작")',
            'mdc-button:has-text("Start meeting")',
            'mdc-button:has-text("Start Meeting")',
            'mdc-button:has-text("미팅 시작")',
            'mdc-button:has-text("회의 시작")',
            'role=button[name=/Start meeting/i]',
            'role=button[name=/미팅 시작/]',
            'role=button[name=/회의 시작/]',
            'text="Start meeting"',
            'text="Start Meeting"',
            'text="미팅 시작"',
            'text="회의 시작"',
            '[aria-label*="Start meeting"]',
            '[aria-label*="미팅 시작"]',
            '[aria-label*="회의 시작"]',
        ],
        "join_button": [
            'button:has-text("Join meeting")',
            'button:has-text("Join Meeting")',
            'button:has-text("Join webinar")',
            'button:has-text("Join")',
            'button:has-text("미팅 참여")',
            'button:has-text("회의 참여")',
            'button:has-text("참여")',
            'button:has-text("참가")',
            '[role="button"]:has-text("Join meeting")',
            '[role="button"]:has-text("Join Meeting")',
            '[role="button"]:has-text("Join")',
            '[role="button"]:has-text("미팅 참여")',
            '[role="button"]:has-text("회의 참여")',
            '[role="button"]:has-text("참여")',
            '[role="button"]:has-text("참가")',
            'mdc-button:has-text("Join meeting")',
            'mdc-button:has-text("Join Meeting")',
            'mdc-button:has-text("Join")',
            'mdc-button:has-text("미팅 참여")',
            'mdc-button:has-text("회의 참여")',
            'mdc-button:has-text("참여")',
            'mdc-button:has-text("참가")',
            'role=button[name=/Join meeting/i]',
            'role=button[name=/Join/i]',
            'role=button[name=/미팅 참여/]',
            'role=button[name=/회의 참여/]',
            'role=button[name=/참여/]',
            'role=button[name=/참가/]',
            'text="Join meeting"',
            'text="Join Meeting"',
            'text="Join"',
            'text="미팅 참여"',
            'text="회의 참여"',
            'text="참여"',
            'text="참가"',
            '[aria-label*="Join meeting"]',
            '[aria-label="Join"]',
            'button:has-text("미팅 시작")',
            'button:has-text("회의 시작")',
            '[role="button"]:has-text("미팅 시작")',
            '[role="button"]:has-text("회의 시작")',
            'mdc-button:has-text("미팅 시작")',
            'mdc-button:has-text("회의 시작")',
            'role=button[name=/미팅 시작/]',
            'role=button[name=/회의 시작/]',
            'text="미팅 시작"',
            'text="회의 시작"',
            '[aria-label*="미팅 참여"]',
            '[aria-label*="회의 참여"]',
            '[aria-label*="참여"]',
            '[aria-label*="참가"]',
            '[aria-label*="미팅 시작"]',
            '[aria-label*="회의 시작"]',
        ],
        "final_join_fast": [
            'text="미팅 참여"',
            'text="회의 참여"',
            'text="Join meeting"',
            'text="Join Meeting"',
            '[role="button"]:has-text("미팅 참여")',
            '[role="button"]:has-text("회의 참여")',
            '[role="button"]:has-text("Join meeting")',
            '[role="button"]:has-text("Join Meeting")',
            'mdc-button:has-text("미팅 참여")',
            'mdc-button:has-text("회의 참여")',
            'mdc-button:has-text("Join meeting")',
            'mdc-button:has-text("Join Meeting")',
            'button:has-text("미팅 참여")',
            'button:has-text("회의 참여")',
            'button:has-text("Join meeting")',
            'button:has-text("Join Meeting")',
            'role=button[name=/미팅 참여/]',
            'role=button[name=/회의 참여/]',
            'role=button[name=/Join meeting/i]',
            'role=button[name=/Join Meeting/i]',
        ],
        "joined_indicator": [
            'text=/미팅 중/',
            '[aria-label*="Leave meeting"]',
            '[aria-label*="Leave"]',
            '[aria-label*="미팅 나가기"]',
            '[aria-label*="나가기"]',
            'button:has-text("Leave")',
            'button:has-text("나가기")',
            '[data-test*="leave"]',
            '[aria-label*="Mute"]',
            '[aria-label*="Unmute"]',
            '[aria-label*="음소거"]',
        ],
        "joined": [
            'text=/미팅 중/',
            '[aria-label*="Leave meeting"]',
            '[aria-label*="Leave"]',
            '[aria-label*="미팅 나가기"]',
            '[aria-label*="나가기"]',
            'button:has-text("Leave")',
            'button:has-text("나가기")',
            '[data-test*="leave"]',
            '[aria-label*="Mute"]',
            '[aria-label*="Unmute"]',
            '[aria-label*="음소거"]',
        ],
        "lobby_indicator": [
            'text=/waiting for.*(host|organizer)/i',
            'text=/host.*(let|admit).*you in/i',
            'text=/you are in the lobby/i',
            'text=/waiting room/i',
            '[data-test*="lobby"]',
        ],
        "blocked_indicator": [
            'text=/meeting has not started/i',
            'text=/unable to join/i',
            'text=/cannot join/i',
            'text=/meeting is locked/i',
            'text=/removed from the meeting/i',
            '[data-test*="error"]',
        ],
        "waiting_for_others_indicator": [
            'text="다른 사용자가 참여할 때까지 기다리는 중"',
            'text="다른 사용자가 참여할 때까지"',
            'text="다른 사람이 참여할 때까지 기다리는 중"',
            'text="참여할 때까지 기다리는 중"',
            'text="기다리는 중"',
            'text="Waiting for others to join"',
            'text="Waiting for others"',
            "text=\"You're the only one here\"",
            'text="You are the only one here"',
            'text="No one else is here"',
            'text=/Waiting for others to join/i',
            'text=/Waiting for others/i',
            "text=/You're the only one here/i",
            'text=/You are the only one here/i',
            'text=/No one else is here/i',
        ],
        "mic_enable": [
            '[aria-label*="Unmute"]',
            'button:has-text("Unmute")',
            '[data-test*="unmute"]',
        ],
        "mic_disable": [
            '[aria-label*="Mute"]',
            'button:has-text("Mute")',
            '[data-test*="mute"]',
        ],
        "mic_on_indicator": [
            'role=button[name=/Mute/i]',
            'role=button[name=/Mute microphone/i]',
            '[aria-label*="Mute"]',
            'role=button[name=/음소거/]',
            'role=button[name=/마이크 음소거/]',
            '[aria-label*="음소거"]',
        ],
        "mic_off_indicator": [
            'role=button[name=/Unmute/i]',
            'role=button[name=/Unmute microphone/i]',
            '[aria-label*="Unmute"]',
            'role=button[name=/음소거 해제/]',
            'role=button[name=/마이크 음소거 해제/]',
            '[aria-label*="음소거 해제"]',
        ],
        "camera_enable": [
            '[aria-label*="Start video"]',
            '[aria-label*="Turn on camera"]',
            'button:has-text("Start video")',
            '[data-test*="start-video"]',
        ],
        "camera_disable": [
            '[aria-label*="Stop video"]',
            '[aria-label*="Turn off camera"]',
            'button:has-text("Stop video")',
            '[data-test*="stop-video"]',
        ],
        "camera_on_indicator": [
            'role=button[name=/Stop video/i]',
            'role=button[name=/Turn off camera/i]',
            '[aria-label*="Stop video"]',
            'role=button[name=/비디오 중지/]',
            'role=button[name=/카메라 끄기/]',
            '[aria-label*="비디오 중지"]',
            '[aria-label*="카메라 끄기"]',
        ],
        "camera_off_indicator": [
            'role=button[name=/Start video/i]',
            'role=button[name=/Turn on camera/i]',
            '[aria-label*="Start video"]',
            'role=button[name=/비디오 시작/]',
            'role=button[name=/카메라 켜기/]',
            '[aria-label*="비디오 시작"]',
            '[aria-label*="카메라 켜기"]',
        ],
        "prejoin_mic_on_indicator": [
            'role=button[name=/Mute/i]',
            'role=button[name=/Mute microphone/i]',
            '[aria-label*="Mute"]',
            'role=button[name=/음소거/]',
            'role=button[name=/마이크 음소거/]',
            '[aria-label*="음소거"]',
        ],
        "prejoin_mic_off_indicator": [
            'role=button[name=/Unmute/i]',
            'role=button[name=/Unmute microphone/i]',
            '[aria-label*="Unmute"]',
            'role=button[name=/음소거 해제/]',
            'role=button[name=/마이크 음소거 해제/]',
            '[aria-label*="음소거 해제"]',
        ],
        "prejoin_camera_on_indicator": [
            'role=button[name=/Stop video/i]',
            'role=button[name=/Turn off camera/i]',
            '[aria-label*="Stop video"]',
            'role=button[name=/비디오 중지/]',
            'role=button[name=/카메라 끄기/]',
            '[aria-label*="비디오 중지"]',
            '[aria-label*="카메라 끄기"]',
        ],
        "prejoin_camera_off_indicator": [
            'role=button[name=/Start video/i]',
            'role=button[name=/Turn on camera/i]',
            '[aria-label*="Start video"]',
            'role=button[name=/비디오 시작/]',
            'role=button[name=/카메라 켜기/]',
            '[aria-label*="비디오 시작"]',
            '[aria-label*="카메라 켜기"]',
        ],
        "share_start": [
            '[aria-label*="Share content"]',
            '[aria-label*="Share screen"]',
            'button:has-text("Share")',
            '[data-test*="share"]',
        ],
        "share_stop": [
            '[aria-label*="Stop sharing"]',
            'button:has-text("Stop sharing")',
            '[data-test*="stop-share"]',
        ],
        "screen_share_button": [
            'role=button[name=/Share content/i]',
            'role=button[name=/Share screen/i]',
            'role=button[name=/화면 공유/]',
            'role=button[name=/콘텐츠 공유/]',
            'role=button[name=/공유/]',
            '[aria-label*="Share"]',
            '[aria-label*="화면 공유"]',
            '[aria-label*="콘텐츠 공유"]',
            '[aria-label*="공유"]',
        ],
        "screen_share_on_indicator": [
            'role=button[name=/Stop sharing/i]',
            'text=/You are sharing/i',
            'role=button[name=/공유 중지/]',
            'text=/공유를 중지/',
            'text=/공유하고 있습니다/',
            '[aria-label*="Stop sharing"]',
            '[aria-label*="공유 중지"]',
        ],
        "screen_share_stop": [
            'role=button[name=/Stop sharing/i]',
            'role=button[name=/공유 중지/]',
            '[aria-label*="Stop sharing"]',
            '[aria-label*="공유 중지"]',
        ],
        "share_target": [
            'button:has-text("Screen")',
            '[aria-label*="Screen"]',
            '[aria-label*="Entire screen"]',
        ],
        "share_confirm": [
            'button:has-text("Share")',
            'button:has-text("Allow")',
        ],
        "leave_button": [
            '[aria-label*="Leave meeting"]',
            '[aria-label*="Leave"]',
            'button:has-text("Leave")',
            '[data-test*="leave"]',
        ],
        "leave_confirm": [
            'button:has-text("Leave meeting")',
            'button:has-text("Leave")',
            'button:has-text("End meeting")',
        ],
    }

    def __init__(self, config: Mapping[str, Any]):
        super().__init__(config)
        adapter_config = self.adapter_config()
        if isinstance(adapter_config, dict):
            adapter_config.setdefault("apply_initial_media_state_in_prejoin", True)
            adapter_config.setdefault("strict_prejoin_media_state", False)
            adapter_config.setdefault("fail_on_prejoin_media_state_unverified", False)
            adapter_config.setdefault("post_join_media_check", False)
            adapter_config.setdefault("strict_post_join_media_state", False)
            adapter_config.setdefault("fail_on_post_join_media_unverified", False)
            adapter_config.setdefault("dismiss_external_protocol_dialog", True)
            adapter_config.setdefault("external_protocol_dismiss_method", "auto")
            adapter_config.setdefault("strict_external_protocol_dismiss", False)
            adapter_config.setdefault("suppress_external_protocol_dialog", True)
            adapter_config.setdefault("use_persistent_browser_profile_for_webex", True)
            adapter_config.setdefault(
                "blocked_external_protocol_schemes",
                [
                    "webex",
                    "wbx",
                    "ciscospark",
                    "webexteams",
                    "webexapp",
                    "webex-meetings",
                    "cisco-webex",
                ],
            )
            adapter_config.setdefault("protocol_handler_excluded_scheme_value", True)
            adapter_config.setdefault("retry_navigation_on_page_closed", True)
            adapter_config.setdefault("max_navigation_retries", 1)
            adapter_config.setdefault("max_download_retry_attempts", 3)
            adapter_config.setdefault("download_retry_settle_ms", 2000)
            adapter_config.setdefault("download_retry_click_total_timeout_sec", 5.0)
            adapter_config.setdefault("webclient_frame_ready_wait_sec", 15.0)
            adapter_config.setdefault("page_state_detection_timeout_sec", 3.0)
            adapter_config.setdefault("frame_snapshot_timeout_sec", 0.5)
        self._playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.mic_enabled = bool(adapter_config.get("initial_microphone_enabled", True))
        self.camera_enabled = bool(adapter_config.get("initial_camera_enabled", True))
        self.screen_sharing = bool(adapter_config.get("initial_screen_sharing", False))
        self._meeting_joined_notified = False
        self._media_ready_notified = False
        self.browser_log = []
        self.sanity_check_results = []
        self.external_protocol_dismiss_attempts = []
        self.external_protocol_profile_path = None
        self._last_page_metadata_error = None
        self._last_display_name_fill_method = None
        self._preferred_webex_meeting_frame = None
        self._browser_join_clicked_once = False
        self._final_join_clicked_success = False
        self.joined = False
        self.in_meeting = False
        self._webex_probe_tasks = set()
        self._empty_display_name_frame_attempts = set()

    def _progress(self, stage, details=None):
        payload = {"stage": stage, **dict(details or {})}
        print(f"webex_progress {json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)}", flush=True)
        emit_event(self.config, stage, dict(details or {}), self.service_name)

    def adapter_config(self):
        return self.config.get("adapter_config", {})

    def browser_args(self):
        adapter_config = self.adapter_config()
        configured = adapter_config.get("browser_args")
        args = list(configured or super().browser_args())
        required = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--autoplay-policy=no-user-gesture-required",
            "--use-fake-ui-for-media-stream",
            "--window-size=1280,720",
            "--lang=en-US",
        ]
        if adapter_config.get("ignore_certificate_errors") is True:
            required.append("--ignore-certificate-errors")
        if adapter_config.get("disable_gpu") is True:
            required.extend(["--disable-gpu", "--disable-gpu-compositing"])

        share_target = adapter_config.get("screen_share_target") or adapter_config.get("auto_select_desktop_capture_source")
        if share_target:
            required.append(f"--auto-select-desktop-capture-source={share_target}")

        for arg in required:
            if not self._has_browser_arg(args, arg):
                args.append(arg)

        for arg in adapter_config.get("extra_browser_args") or []:
            if not self._has_browser_arg(args, str(arg)):
                args.append(str(arg))
        return args

    def launch_options(self):
        adapter_config = self.adapter_config()
        options = {
            "args": self.browser_args(),
            "headless": bool(adapter_config.get("headless", False)),
        }

        display = self._configured_display()
        if display:
            env = dict(os.environ)
            env["DISPLAY"] = display
            options["env"] = env

        executable_path = (
            adapter_config.get("browser_executable_path")
            or adapter_config.get("chromium_executable_path")
        )
        if executable_path:
            options["executable_path"] = str(executable_path)
        elif adapter_config.get("browser_channel"):
            options["channel"] = str(adapter_config["browser_channel"])
        else:
            discovered = self._discover_browser_executable()
            if discovered:
                options["executable_path"] = discovered

        slow_mo = adapter_config.get("slow_mo")
        if slow_mo is not None:
            options["slow_mo"] = slow_mo

        return options

    def persistent_context_options(self):
        options = dict(self.launch_options())
        options.update(self.context_options())
        return options

    def _use_persistent_browser_profile(self):
        return bool(self.adapter_config().get("use_persistent_browser_profile_for_webex", True))

    def _external_protocol_suppression_enabled(self):
        return bool(self.adapter_config().get("suppress_external_protocol_dialog", True))

    def _chrome_user_data_dir(self):
        adapter_config = self.adapter_config()
        configured = adapter_config.get("chrome_user_data_dir") or adapter_config.get("user_data_dir")
        if configured:
            return Path(str(configured)).expanduser()

        bot = self.config.get("bot", {})
        if not isinstance(bot, Mapping):
            bot = {}
        bot_name = (
            adapter_config.get("bot_id")
            or adapter_config.get("display_name")
            or self.config.get("bot_name")
            or self.config.get("client_id")
            or bot.get("display_name")
            or "default"
        )
        safe_bot_name = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(bot_name)).strip("_")
        return Path("artifacts/webex_chrome_profile") / (safe_bot_name or "default")

    def _prepare_external_protocol_suppression_profile(self):
        user_data_dir = self._chrome_user_data_dir()
        default_dir = user_data_dir / "Default"
        default_dir.mkdir(parents=True, exist_ok=True)

        if not self._external_protocol_suppression_enabled():
            self.external_protocol_profile_path = str(user_data_dir)
            return user_data_dir

        excluded_schemes = self._protocol_handler_excluded_schemes()
        for path in (user_data_dir / "Local State", default_dir / "Preferences"):
            self._merge_chrome_json_file(path, excluded_schemes)

        self.external_protocol_profile_path = str(user_data_dir)
        details = {
            "profile_path": str(user_data_dir),
            "excluded_schemes": excluded_schemes,
            "persistent_context": self._use_persistent_browser_profile(),
        }
        self._append_browser_log(
            "webex_external_protocol_suppression_profile_prepared",
            json.dumps(details, sort_keys=True, default=str),
        )
        emit_event(
            self.config,
            "webex_external_protocol_suppression_profile_prepared",
            details,
            self.service_name,
        )
        return user_data_dir

    def _protocol_handler_excluded_schemes(self):
        value = bool(self.adapter_config().get("protocol_handler_excluded_scheme_value", True))
        schemes = self.adapter_config().get("blocked_external_protocol_schemes") or []
        return {str(scheme): value for scheme in schemes if str(scheme)}

    def _merge_chrome_json_file(self, path, excluded_schemes):
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8") or "{}")
                if not isinstance(data, dict):
                    data = {}
            except Exception as exc:
                self._append_browser_log("chrome_profile_json_read_failed", f"{path}: {exc!r}")
                data = {}

        protocol_handler = data.setdefault("protocol_handler", {})
        if not isinstance(protocol_handler, dict):
            protocol_handler = {}
            data["protocol_handler"] = protocol_handler
        existing = protocol_handler.setdefault("excluded_schemes", {})
        if not isinstance(existing, dict):
            existing = {}
            protocol_handler["excluded_schemes"] = existing
        existing.update(excluded_schemes)

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    def selectors(self, name):
        configured = self.adapter_config().get("selectors", {})
        if isinstance(configured, Mapping) and name in configured:
            value = configured[name]
            return value if isinstance(value, list) else [value]
        aliases = {
            "join_from_browser": "browser_join",
            "browser_join": "join_from_browser",
            "join_as_guest": "guest_join",
            "guest_join": "join_as_guest",
            "display_name": "name_input",
            "name_input": "display_name",
            "joined_indicator": "joined",
            "joined": "joined_indicator",
            "mic_on_indicator": "mic_disable",
            "mic_off_indicator": "mic_enable",
            "camera_on_indicator": "camera_disable",
            "camera_off_indicator": "camera_enable",
            "screen_share_button": "share_start",
            "screen_share_on_indicator": "share_stop",
            "screen_share_stop": "share_stop",
        }
        alias = aliases.get(name)
        if isinstance(configured, Mapping) and alias in configured:
            value = configured[alias]
            return value if isinstance(value, list) else [value]
        if name == "browser_join":
            return list(self.DEFAULT_SELECTORS.get("join_from_browser", []))
        return list(self.DEFAULT_SELECTORS.get(name, []))

    def timeout_ms(self, key, default):
        try:
            return int(self.adapter_config().get(key, default))
        except (TypeError, ValueError):
            return default

    async def launch(self):
        if self.page is not None:
            return None

        from playwright.async_api import async_playwright

        self._progress("webex_launch_start")
        if not self.adapter_config().get("skip_sanity_checks", False):
            self.run_sanity_checks()
        self._playwright = await async_playwright().start()
        if self._use_persistent_browser_profile():
            user_data_dir = self._prepare_external_protocol_suppression_profile()
            options = self.persistent_context_options()
            self.context = await self._playwright.chromium.launch_persistent_context(
                str(user_data_dir),
                **options,
            )
            self.browser = getattr(self.context, "browser", None)
            if callable(self.browser):
                self.browser = self.browser()
            self._attach_browser_logging(self.browser)
        else:
            self._prepare_external_protocol_suppression_profile()
            self.browser = await self._playwright.chromium.launch(**self.launch_options())
            self._attach_browser_logging(self.browser)
            self.context = await self.browser.new_context(**self.context_options())
        self._attach_context_logging(self.context)
        self.page = await self.context.new_page()
        self._attach_page_logging(self.page)
        emit_event(self.config, "adapter_launched", {"backend": "playwright"}, self.service_name)
        return None

    async def connect(self, vtc_url, display_name=None):
        if isinstance(vtc_url, (int, float)) and display_name is None:
            duration = vtc_url
            await self.launch()
            await self.connect_to_meeting(
                str(self.config["vtc_url"]),
                self._display_name(),
            )
            await asyncio.sleep(duration * 60)
            await self.leave()
            await self.close()
            return f"{self.config.get('bot_name') or 'client'} connected to {self.service_name}."

        await self.launch()
        await self.connect_to_meeting(
            str(vtc_url),
            str(display_name or self._display_name()),
        )
        return True

    async def connect_to_meeting(self, vtc_url: str, display_name: str):
        if self.page is None:
            await self.launch()
        self._meeting_joined_notified = False
        self._media_ready_notified = False
        emit_event(self.config, "webex_join_start", {"vtc_url": vtc_url}, self.service_name)
        emit_event(self.config, "meeting_join_start", {"vtc_url": vtc_url}, self.service_name)

        self._progress("webex_page_goto_start", {"vtc_url": vtc_url})
        page_metadata = await self._navigate_to_meeting_page(vtc_url)
        self._progress(
            "webex_page_opened",
            {"vtc_url": vtc_url, "title": page_metadata["title"], "url": page_metadata["url"]},
        )

        page_load_wait_sec = float(self.adapter_config().get("page_load_wait_sec", 2))
        if page_load_wait_sec > 0:
            await asyncio.sleep(page_load_wait_sec)
            await self._dismiss_external_protocol_prompt(stage="after_goto_settle")

        prejoin_result = await self._run_prejoin_transition_loop(display_name)
        if self._is_terminal_join_state(prejoin_result):
            return await self._handle_join_result(vtc_url, prejoin_result)

        existing_result = await self._wait_for_join_result(0)
        if self._is_terminal_join_state(existing_result):
            return await self._handle_join_result(vtc_url, existing_result)

        if prejoin_result["status"] == "timeout":
            diagnostic_stage = prejoin_result.get("stage") or "webex_prejoin_failed"
            diagnostics = await self._maybe_await(
                self.collect_diagnostics(
                    stage=diagnostic_stage,
                    extra={
                        "join_result": prejoin_result,
                        "url": self._safe_page_url(),
                        "title": await self._safe_page_title(),
                    },
                )
            )
            raise RuntimeError(
                f"Webex prejoin timed out before final Join button ({diagnostic_stage}). "
                f"Visible text: {prejoin_result.get('visible_text', '')}. Diagnostics: {diagnostics}"
            )

        if prejoin_result["status"] == "final_join":
            self._progress("final_join_button_seen", {"selector": prejoin_result.get("selector")})
        join_selector = await self._click_final_join_control(display_name)
        self._final_join_clicked_success = True
        self._progress("final_join_clicked", {"selector": join_selector, "success": True})

        result = await self._wait_for_post_final_join_result(
            float(
                self.adapter_config().get(
                    "post_final_join_result_timeout_sec",
                    self.adapter_config().get(
                        "join_result_timeout_sec",
                        self.timeout_ms("joined_timeout_ms", 45000) / 1000,
                    ),
                )
            )
        )
        return await self._handle_join_result(vtc_url, result)

    async def _navigate_to_meeting_page(self, vtc_url):
        retries = self.timeout_ms("max_navigation_retries", 1)
        if not bool(self.adapter_config().get("retry_navigation_on_page_closed", True)):
            retries = 0

        for attempt in range(retries + 1):
            if not self._is_page_available():
                if not await self._open_replacement_page():
                    break

            self._last_page_metadata_error = None
            await self.page.goto(
                vtc_url,
                wait_until="domcontentloaded",
                timeout=self.timeout_ms("navigation_timeout_ms", 45000),
            )
            await self._dismiss_external_protocol_prompt(stage="after_goto")

            title = await self._safe_page_title()
            url = self._safe_page_url()
            if self._is_page_available() and not self._page_metadata_saw_target_closed():
                return {"title": title, "url": url}

            await self._handle_page_closed_after_goto(vtc_url, title, url, attempt, retries)

        raise RuntimeError("Webex page closed after navigation before prejoin flow could start")

    async def _handle_page_closed_after_goto(self, vtc_url, title, url, attempt, retries):
        details = {
            "vtc_url": vtc_url,
            "title": title,
            "url": url,
            "attempt": attempt,
            "max_navigation_retries": retries,
            "browser_log": list(self.browser_log),
        }
        self._append_browser_log("webex_page_closed_after_goto", json.dumps(details, default=str))
        await self._maybe_await(self.collect_diagnostics(stage="webex_page_closed_after_goto", extra=details))
        if attempt < retries:
            if await self._open_replacement_page():
                emit_event(self.config, "webex_navigation_retry", details, self.service_name)
                return
        raise RuntimeError("Webex page closed after navigation before prejoin flow could start")

    async def _open_replacement_page(self):
        if self.context is None:
            return False
        try:
            if self.browser is not None and hasattr(self.browser, "is_connected"):
                connected = await self._maybe_await(self.browser.is_connected())
                if not connected:
                    return False
            self.page = await self._maybe_await(self.context.new_page())
            self._attach_page_logging(self.page)
            self._append_browser_log("page_recreated", "created replacement page after close")
            return True
        except self._safe_playwright_errors() as exc:
            self._append_browser_log("page_recreate_failed", repr(exc))
            return False
        except Exception as exc:
            self._append_browser_log("page_recreate_failed", repr(exc))
            return False

    async def _handle_join_result(self, vtc_url, result):
        result = self._normalize_join_state(result)
        emit_event(self.config, "webex_join_result", result, self.service_name)

        if result["status"] == "joined":
            self._progress("joined_detected", {"selector": result.get("selector")})
            if hasattr(self, "joined"):
                self.joined = True
            if hasattr(self, "in_meeting"):
                self.in_meeting = True
            emit_event(self.config, "webex_joined_meeting", {"vtc_url": vtc_url, "selector": result.get("selector")}, self.service_name)
            self._notify_meeting_joined(vtc_url)
            media_ready = await self._maybe_run_post_join_media_check(vtc_url)
            status = {
                "status": "joined",
                "selector": result.get("selector"),
                "visible_text": result.get("visible_text", ""),
                "media_ready": media_ready,
            }
            self._progress("webex_join_success", status)
            return status

        if result["status"] == "waiting_for_others":
            self._progress(
                "webex_waiting_for_others_detected",
                {
                    "selector": result.get("selector"),
                    "scope": result.get("scope"),
                    "url": result.get("url"),
                    "title": result.get("title"),
                    "source": result.get("source"),
                },
            )
            if hasattr(self, "joined"):
                self.joined = True
            if hasattr(self, "in_meeting"):
                self.in_meeting = True
            emit_event(self.config, "webex_waiting_for_others_detected", result, self.service_name)
            self._notify_meeting_joined(vtc_url)
            status = {
                "status": "waiting_for_others",
                "selector": result.get("selector"),
                "joined_selector": result.get("joined_selector"),
                "scope": result.get("scope"),
                "url": result.get("url"),
                "title": result.get("title"),
                "source": result.get("source"),
                "leave_control_present": bool(result.get("joined_selector")),
                "visible_text": result.get("visible_text", ""),
                "media_ready": False,
            }
            self._progress("webex_join_success", status)
            return status

        if result["status"] == "waiting_for_host":
            self._progress(
                "webex_waiting_for_host_detected",
                {
                    "selector": result.get("selector"),
                    "scope": result.get("scope"),
                    "url": result.get("url"),
                    "title": result.get("title"),
                    "source": result.get("source"),
                },
            )
            if hasattr(self, "joined"):
                self.joined = True
            if hasattr(self, "in_meeting"):
                self.in_meeting = True
            emit_event(self.config, "webex_waiting_for_host_detected", result, self.service_name)
            self._notify_meeting_joined(vtc_url)
            status = {
                "status": "waiting_for_host",
                "selector": result.get("selector"),
                "joined_selector": result.get("joined_selector"),
                "scope": result.get("scope"),
                "url": result.get("url"),
                "title": result.get("title"),
                "source": result.get("source"),
                "leave_control_present": bool(result.get("joined_selector")),
                "visible_text": result.get("visible_text", ""),
                "media_ready": False,
            }
            self._progress("webex_join_success", status)
            return status

        if result["status"] == "lobby":
            self._progress("lobby_detected", {"selector": result.get("selector")})
            if result.get("source") == "post_final_join" or self.adapter_config().get("accept_lobby_as_joined") is True:
                self._progress(
                    "webex_lobby_detected",
                    {
                        "selector": result.get("selector"),
                        "scope": result.get("scope"),
                        "url": result.get("url"),
                        "title": result.get("title"),
                        "source": result.get("source"),
                    },
                )
                self._notify_meeting_joined(vtc_url)
                if self.adapter_config().get("allow_lobby_media_ready") is True:
                    self._notify_media_ready(vtc_url)
                status = {
                    "status": "lobby",
                    "selector": result.get("selector"),
                    "joined_selector": result.get("joined_selector"),
                    "scope": result.get("scope"),
                    "url": result.get("url"),
                    "title": result.get("title"),
                    "source": result.get("source"),
                    "leave_control_present": bool(result.get("joined_selector")),
                    "visible_text": result.get("visible_text", ""),
                    "media_ready": False,
                }
                self._progress("webex_join_success", status)
                return status
            diagnostics = await self._maybe_await(
                self.collect_diagnostics(stage="lobby", extra={"join_result": result})
            )
            raise RuntimeError(
                f"Webex join stopped in lobby/waiting screen. Visible text: {result.get('visible_text', '')}"
            )

        if result["status"] == "blocked":
            self._progress("blocked_detected", {"selector": result.get("selector")})

        if result["status"] == "timeout":
            self._progress("join_timeout", {"visible_text": result.get("visible_text", "")})
        stage = result.get("stage") or ("webex_join_result_timeout" if result["status"] == "timeout" else f"join_{result['status']}")
        diagnostics = await self._maybe_await(
            self.collect_diagnostics(stage=stage, extra={"join_result": result})
        )
        raise RuntimeError(
            f"Webex join failed with status {result['status']}. "
            f"Visible text: {result.get('visible_text', '')}. Diagnostics: {diagnostics}"
        )

    def _is_terminal_join_state(self, state):
        if not isinstance(state, Mapping):
            return False
        return bool(state.get("joined")) or state.get("status") in {"joined", "waiting_for_others", "waiting_for_host", "lobby", "blocked"}

    def _terminal_join_state_from_fill_result(self, fill_result):
        if not isinstance(fill_result, Mapping):
            return None
        join_state = fill_result.get("join_state")
        return join_state if self._is_terminal_join_state(join_state) else None

    def _normalize_join_state(self, state):
        if not isinstance(state, Mapping):
            return state
        normalized = dict(state)
        status = normalized.get("status")
        if status in {"joined", "waiting_for_others", "waiting_for_host"}:
            normalized["joined"] = True
            normalized.setdefault(
                "joined_source",
                normalized.get("source")
                or normalized.get("action")
                or normalized.get("joined_selector")
                or normalized.get("selector"),
            )
        elif status in {"lobby", "blocked"}:
            normalized.setdefault("joined", False)
        return normalized

    async def _run_prejoin_transition_loop(self, display_name):
        deadline = asyncio.get_running_loop().time() + (
            self.timeout_ms("prejoin_timeout_ms", 30000) / 1000
        )
        timeout = self.timeout_ms("optional_selector_timeout_ms", 1000)
        email = self.adapter_config().get("email")
        password = self.adapter_config().get("password") or self.adapter_config().get("meeting_password")
        download_retry_attempts = 0
        iteration = 0

        while asyncio.get_running_loop().time() < deadline:
            iteration += 1
            self._progress("webex_prejoin_loop_iteration_start", {"iteration": iteration})
            await self._dismiss_external_protocol_prompt(stage="prejoin_loop")
            joined_state = await self._scan_joined_state_all_surfaces(timeout_ms=250)
            if self._is_terminal_join_state(joined_state):
                return joined_state
            state = await self._prejoin_state(timeout_ms=250)
            if self._is_terminal_join_state(state):
                return state

            progressed = False
            download_retry_result = await self._handle_download_retry_page(timeout_ms=timeout)
            if download_retry_result.get("detected"):
                if download_retry_result.get("webclient_frame_detected"):
                    progressed = True
                else:
                    download_retry_attempts += 1
                if download_retry_result.get("browser_join_clicked"):
                    post_browser_join_state = await self._post_browser_join_transition_loop(
                        display_name,
                        timeout_ms=timeout,
                    )
                    if post_browser_join_state["status"] == "final_join" or self._is_terminal_join_state(post_browser_join_state):
                        return post_browser_join_state
                    if post_browser_join_state["status"] == "timeout":
                        return post_browser_join_state
                    progressed = True
                    continue
                if download_retry_result.get("clicked"):
                    await self._dismiss_external_protocol_prompt(stage="after_download_retry_try_again")
                    self._discard_browser_join_snapshot_state()
                    await asyncio.sleep(self._download_retry_settle_sec())
                    if await self._browser_join_after_retry_seen(timeout_ms=timeout):
                        self._progress("webex_browser_join_after_retry_seen")
                if (
                    not download_retry_result.get("webclient_frame_detected")
                    and download_retry_attempts >= self.timeout_ms("max_download_retry_attempts", 3)
                ):
                    still_visible = await self._download_retry_page_visible(timeout_ms=timeout)
                    if still_visible:
                        await self._raise_download_retry_page_timeout(
                            {
                                **download_retry_result,
                                "attempt": download_retry_attempts,
                                "max_attempts": self.timeout_ms("max_download_retry_attempts", 3),
                            }
                        )
                    continue
                if download_retry_result.get("clicked"):
                    continue
                progressed = True
            if await self._name_entry_text_visible():
                self._progress("display_name_page_detected")
                if await self._fill_display_name(display_name, timeout_ms=timeout):
                    progressed = True
                    state = await self._prejoin_state(timeout_ms=75)
                    if state["status"] == "final_join" or self._is_terminal_join_state(state):
                        return state
            progressed = bool(await self._click_first_visible("cookie_close", timeout_ms=timeout)) or progressed
            fill_result = await self._fill_display_name_if_needed(display_name, timeout_ms=timeout)
            terminal_state = self._terminal_join_state_from_fill_result(fill_result)
            if terminal_state:
                return terminal_state
            if fill_result["success"]:
                progressed = True
                state = await self._prejoin_state(timeout_ms=75)
                if state["status"] == "final_join" or self._is_terminal_join_state(state):
                    return state
            progressed = bool(await self._click_first_visible("cookie_reject", timeout_ms=timeout)) or progressed
            fill_result = await self._fill_display_name_if_needed(display_name, timeout_ms=timeout)
            terminal_state = self._terminal_join_state_from_fill_result(fill_result)
            if terminal_state:
                return terminal_state
            if fill_result["success"]:
                progressed = True
                state = await self._prejoin_state(timeout_ms=75)
                if state["status"] == "final_join" or self._is_terminal_join_state(state):
                    return state
            progressed = bool(await self._click_first_visible("cookie_accept", timeout_ms=timeout)) or progressed
            fill_result = await self._fill_display_name_if_needed(display_name, timeout_ms=timeout)
            terminal_state = self._terminal_join_state_from_fill_result(fill_result)
            if terminal_state:
                return terminal_state
            if fill_result["success"]:
                progressed = True
                state = await self._prejoin_state(timeout_ms=75)
                if state["status"] == "final_join" or self._is_terminal_join_state(state):
                    return state
            progressed = bool(await self._click_browser_prejoin_selector("cancel_open_app_prompt", timeout_ms=timeout)) or progressed
            await self._dismiss_external_protocol_prompt(stage="before_browser_join")
            fill_result = await self._fill_display_name_if_needed(display_name, timeout_ms=timeout)
            terminal_state = self._terminal_join_state_from_fill_result(fill_result)
            if terminal_state:
                return terminal_state
            if fill_result["success"]:
                progressed = True
                state = await self._prejoin_state(timeout_ms=75)
                if state["status"] == "final_join" or self._is_terminal_join_state(state):
                    return state
            self._progress("browser_join_click_attempt")
            progressed = bool(await self._click_browser_prejoin_selector("join_from_browser", timeout_ms=timeout)) or progressed
            fill_result = await self._fill_display_name_if_needed(display_name, timeout_ms=timeout)
            terminal_state = self._terminal_join_state_from_fill_result(fill_result)
            if terminal_state:
                return terminal_state
            if fill_result["success"]:
                progressed = True
                state = await self._prejoin_state(timeout_ms=75)
                if state["status"] == "final_join" or self._is_terminal_join_state(state):
                    return state
            progressed = bool(await self._click_browser_prejoin_selector("join_from_this_browser", timeout_ms=timeout)) or progressed
            fill_result = await self._fill_display_name_if_needed(display_name, timeout_ms=timeout)
            terminal_state = self._terminal_join_state_from_fill_result(fill_result)
            if terminal_state:
                return terminal_state
            if fill_result["success"]:
                progressed = True
                state = await self._prejoin_state(timeout_ms=75)
                if state["status"] == "final_join" or self._is_terminal_join_state(state):
                    return state
            progressed = bool(await self._click_browser_prejoin_selector("continue_in_browser", timeout_ms=timeout)) or progressed
            fill_result = await self._fill_display_name_if_needed(display_name, timeout_ms=timeout)
            terminal_state = self._terminal_join_state_from_fill_result(fill_result)
            if terminal_state:
                return terminal_state
            if fill_result["success"]:
                progressed = True
                state = await self._prejoin_state(timeout_ms=75)
                if state["status"] == "final_join" or self._is_terminal_join_state(state):
                    return state
            progressed = bool(await self._click_browser_prejoin_selector("use_web_app", timeout_ms=timeout)) or progressed
            fill_result = await self._fill_display_name_if_needed(display_name, timeout_ms=timeout)
            terminal_state = self._terminal_join_state_from_fill_result(fill_result)
            if terminal_state:
                return terminal_state
            if fill_result["success"]:
                progressed = True
                state = await self._prejoin_state(timeout_ms=75)
                if state["status"] == "final_join" or self._is_terminal_join_state(state):
                    return state
            progressed = bool(await self._click_browser_prejoin_selector("open_in_browser", timeout_ms=timeout)) or progressed
            fill_result = await self._fill_display_name_if_needed(display_name, timeout_ms=timeout)
            terminal_state = self._terminal_join_state_from_fill_result(fill_result)
            if terminal_state:
                return terminal_state
            if fill_result["success"]:
                progressed = True
                state = await self._prejoin_state(timeout_ms=75)
                if state["status"] == "final_join" or self._is_terminal_join_state(state):
                    return state
            progressed = bool(await self._click_first_visible("join_as_guest", timeout_ms=timeout)) or progressed
            progressed = bool(await self._fill_display_name(display_name, timeout_ms=timeout)) or progressed
            progressed = bool(await self._fill_first_visible("email_input", email, timeout_ms=timeout)) or progressed
            progressed = bool(await self._fill_first_visible("password_input", password, timeout_ms=timeout)) or progressed
            progressed = bool(await self._click_first_visible("continue_button", timeout_ms=timeout)) or progressed
            progressed = bool(await self._click_first_visible("next_button", timeout_ms=timeout)) or progressed
            progressed = bool(await self._click_first_visible("use_computer_audio", timeout_ms=timeout)) or progressed
            progressed = bool(await self._fill_display_name(display_name, timeout_ms=timeout)) or progressed
            progressed = bool(await self._apply_prejoin_media_preferences()) or progressed

            state = await self._prejoin_state(timeout_ms=250)
            if state["status"] == "final_join" or self._is_terminal_join_state(state):
                return state

            await asyncio.sleep(0.2 if progressed else 0.5)

        await self._dismiss_external_protocol_prompt(stage="prejoin_timeout")
        joined_state = await self._scan_joined_state_all_surfaces(timeout_ms=timeout)
        if self._is_terminal_join_state(joined_state):
            return joined_state
        fill_result = await self._fill_display_name_if_needed(display_name, timeout_ms=timeout, diagnose=True)
        terminal_state = self._terminal_join_state_from_fill_result(fill_result)
        if terminal_state:
            return terminal_state
        visible_text = await self._visible_text_excerpt()
        title = await self._safe_page_title()
        result = {"status": "timeout", "selector": None, "visible_text": visible_text}
        self._progress("join_timeout", {"visible_text": result.get("visible_text", "")})
        download_retry = await self._download_retry_page_state(timeout_ms=timeout)
        extra = {
            "join_result": result,
            "url": self._safe_page_url(),
            "title": title,
            "window_list": self._webex_window_list(),
        }
        stage = self._prejoin_timeout_stage(
            title=title,
            visible_text=visible_text,
            window_list=extra["window_list"],
            download_retry=download_retry,
        )
        if download_retry.get("detected"):
            extra.update(
                {
                    "download_retry": download_retry,
                    "download_retry_keyword_hits": self._download_retry_keyword_hits(result.get("visible_text", "")),
                    "links_buttons_debug": await self._links_buttons_debug_info(),
                }
            )
        await self._maybe_await(
            self.collect_diagnostics(
                stage=stage,
                extra=extra,
            )
        )
        result["stage"] = stage
        return result

    async def _handle_download_retry_page(self, timeout_ms=None):
        state = await self._download_retry_page_state(timeout_ms=timeout_ms)
        if state.get("webclient_frame_ready"):
            frame = state.get("webclient_frame") or {}
            self._progress("webex_webclient_frame_detected", frame)
            self._progress(
                "webex_next_action_selected",
                {"action": "continue_webclient_prejoin", "reason": "ready_webclient_frame"},
            )
            return {
                **state,
                "detected": True,
                "clicked": False,
                "got_it_clicked": False,
            }
        if (state.get("detected") or state.get("webclient_frame_exists")) and state.get("browser_join_action"):
            self._progress(
                "webex_next_action_selected",
                {"action": "click_browser_join", "reason": "outer_browser_join_available"},
            )
            clicked = await self._click_browser_join_from_state(state, timeout_ms=timeout_ms)
            if clicked:
                await self._cancel_stale_probe_tasks(reason="browser_join_click_success")
                self._discard_browser_join_snapshot_state()
                await self._dismiss_external_protocol_prompt(stage="after_browser_join_click")
                return {**state, "detected": True, "clicked": True, "browser_join_clicked": True, "click": clicked}
            self._progress("browser_join_click_not_found", {"browser_join_action": state.get("browser_join_action")})
            await self._raise_browser_join_click_not_found(state)
        if (state.get("detected") or state.get("webclient_frame_exists")) and state.get("try_again_action"):
            self._progress("webex_download_retry_page_detected", state)
            self._progress(
                "webex_next_action_selected",
                {
                    "action": "click_try_again",
                    "reason": (
                        "try_again_available_with_unready_webclient_frame"
                        if state.get("webclient_frame_exists") and not state.get("webclient_frame_ready")
                        else "try_again_available"
                    ),
                },
            )
            got_it, clicked = await self._click_try_again_from_state(state, timeout_ms=timeout_ms)
            if clicked:
                self._progress("webex_try_again_clicked", clicked)
                await self._after_try_again_click_rescan(timeout_ms=timeout_ms)
                return {**state, "detected": True, "clicked": True, "got_it_clicked": got_it, "click": clicked}
            await self._raise_download_retry_try_again_not_clickable({**state, "got_it_clicked": got_it})
        if state.get("webclient_frame_exists"):
            self._progress(
                "webex_next_action_selected",
                {"action": "wait_for_webclient_frame_ready", "reason": "guest_frame_exists_but_not_ready"},
            )
            ready_state = await self._wait_for_webclient_frame_ready(timeout_ms=timeout_ms)
            if ready_state.get("browser_join_clicked"):
                return {**ready_state, "detected": True}
            if ready_state.get("webclient_frame_ready"):
                frame = ready_state.get("webclient_frame") or {}
                self._progress("webex_webclient_frame_detected", frame)
                self._progress(
                    "webex_next_action_selected",
                    {"action": "continue_webclient_prejoin", "reason": "ready_webclient_frame"},
                )
                return {**ready_state, "detected": True, "clicked": False, "got_it_clicked": False}
            await self._raise_webclient_frame_not_ready(ready_state or state)
        if not state.get("detected"):
            self._progress(
                "webex_next_action_selected",
                {"action": "continue_prejoin", "reason": "download_retry_not_detected"},
            )
            return state
        self._progress(
            "webex_next_action_selected",
            {"action": "fail_fast", "reason": "no_actionable_webex_state"},
        )
        await self._raise_no_actionable_webex_state(state)

    async def _raise_no_actionable_webex_state(self, state):
        extra = {
            "download_retry": state,
            "visible_text": await self._visible_text_excerpt(),
            "url": self._safe_page_url(),
            "title": await self._safe_page_title(),
            "links_buttons_debug": await self._links_buttons_debug_info(),
        }
        self._progress("no_actionable_webex_state", extra)
        diagnostics = await self._maybe_await(
            self.collect_diagnostics(stage="no_actionable_webex_state", extra=extra)
        )
        raise RuntimeError(f"no_actionable_webex_state. Diagnostics: {diagnostics}")

    async def _wait_for_webclient_frame_ready(self, timeout_ms=None):
        wait_sec = self._webclient_frame_ready_wait_sec()
        deadline = asyncio.get_running_loop().time() + wait_sec
        last_state = {}
        clicked_browser_join = set()
        self._progress("webex_wait_for_webclient_frame_ready_start", {"timeout_sec": wait_sec})
        while asyncio.get_running_loop().time() < deadline:
            last_state = await self._download_retry_page_state(timeout_ms=timeout_ms)
            if last_state.get("webclient_frame_ready"):
                self._progress("webex_wait_for_webclient_frame_ready_done", {"ready": True})
                return last_state
            action = last_state.get("browser_join_action") or {}
            action_key = (action.get("scope"), action.get("selector"), action.get("text"))
            if action and action_key not in clicked_browser_join:
                self._progress(
                    "webex_next_action_selected",
                    {"action": "click_browser_join", "reason": "outer_browser_join_available"},
                )
                clicked_browser_join.add(action_key)
                clicked = await self._click_browser_join_from_state(last_state, timeout_ms=timeout_ms)
                if clicked:
                    self._browser_join_clicked_once = True
                    await self._cancel_stale_probe_tasks(reason="browser_join_click_success")
                    self._discard_browser_join_snapshot_state()
                    await self._dismiss_external_protocol_prompt(stage="after_browser_join_click")
                    return {
                        **last_state,
                        "detected": True,
                        "clicked": True,
                        "browser_join_clicked": True,
                        "click": clicked,
                    }
            await asyncio.sleep(0.5)
        self._progress("webex_wait_for_webclient_frame_ready_done", {"ready": False, "timeout_sec": wait_sec})
        return last_state

    def _webclient_frame_ready_wait_sec(self):
        try:
            return max(0.1, float(self.adapter_config().get("webclient_frame_ready_wait_sec", 15.0)))
        except (TypeError, ValueError):
            return 15.0

    def _post_browser_join_transition_timeout_sec(self):
        try:
            return max(0.1, float(self.adapter_config().get("post_browser_join_transition_timeout_sec", 45.0)))
        except (TypeError, ValueError):
            return 45.0

    def _discard_browser_join_snapshot_state(self):
        self._preferred_webex_meeting_frame = None
        self._empty_display_name_frame_attempts.clear()

    def _track_probe_task(self, task):
        if task is not None:
            self._webex_probe_tasks.add(task)
        return task

    async def _cancel_stale_probe_tasks(self, reason="phase_transition"):
        tasks = [task for task in list(getattr(self, "_webex_probe_tasks", set())) if task is not None and not task.done()]
        for task in tasks:
            task.cancel()
        results = []
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        self._webex_probe_tasks.difference_update(tasks)
        self._progress(
            "webex_stale_probe_tasks_cancelled",
            {"reason": reason, "task_count": len(tasks), "exception_count": sum(1 for item in results if isinstance(item, BaseException))},
        )
        return results

    async def _post_browser_join_transition_loop(self, display_name, timeout_ms=None):
        timeout_sec = self._post_browser_join_transition_timeout_sec()
        deadline = asyncio.get_running_loop().time() + timeout_sec
        self._progress("webex_post_browser_join_transition_start", {"timeout_sec": timeout_sec})
        await self._cancel_stale_probe_tasks(reason="post_browser_join_transition_start")
        self._discard_browser_join_snapshot_state()
        last_result = {"status": "continue", "selector": None, "visible_text": ""}
        while asyncio.get_running_loop().time() < deadline:
            await self._dismiss_external_protocol_prompt(stage="post_browser_join_transition")
            self._progress("webex_post_browser_join_rescan_start", {"page_count": len(self._known_playwright_pages())})
            result = await self._post_browser_join_rescan(display_name, timeout_ms=timeout_ms)
            last_result = result
            self._progress("webex_post_browser_join_rescan_result", result)
            if result.get("detached") or result.get("retry_rescan"):
                await asyncio.sleep(0.25)
                continue
            if result["status"] == "final_join" or self._is_terminal_join_state(result):
                return result
            await asyncio.sleep(float(self.adapter_config().get("post_browser_join_poll_interval_sec", 0.5)))

        result = {
            "status": "timeout",
            "stage": "webex_post_browser_join_transition_timeout",
            "selector": None,
            "visible_text": await self._visible_text_excerpt(),
            "last_result": last_result,
        }
        self._progress("webex_post_browser_join_transition_timeout", result)
        await self._maybe_await(self.collect_diagnostics(stage="webex_post_browser_join_transition_timeout", extra=result))
        return result

    async def _post_browser_join_rescan(self, display_name, timeout_ms=None):
        joined_state = await self._scan_joined_state_all_surfaces(timeout_ms=timeout_ms or 250)
        if self._is_terminal_join_state(joined_state):
            return joined_state

        state = await self._download_retry_page_state(timeout_ms=timeout_ms)
        if state.get("detached") or state.get("retry_rescan"):
            return {"status": "continue", "detached": True, "retry_rescan": True}
        if state.get("webclient_frame_detected"):
            frame = state.get("webclient_frame") or {}
            self._progress("webex_webclient_frame_detected", frame)

        fill_result = await self._fill_display_name_if_needed(display_name, timeout_ms=timeout_ms)
        terminal_state = self._terminal_join_state_from_fill_result(fill_result)
        if terminal_state:
            return terminal_state

        prejoin_state = await self._prejoin_state(timeout_ms=timeout_ms or 250)
        if prejoin_state["status"] == "final_join" or self._is_terminal_join_state(prejoin_state):
            return prejoin_state
        if fill_result.get("success"):
            return {"status": "continue", "selector": fill_result.get("selector"), "display_name_filled": True, "visible_text": await self._visible_text_excerpt()}
        return prejoin_state

    async def _download_retry_page_state(self, timeout_ms=None):
        timeout_sec = self._page_state_detection_timeout_sec()
        try:
            return await asyncio.wait_for(
                self._download_retry_page_state_from_snapshot(timeout_ms=timeout_ms),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            extra = {
                "timeout_sec": timeout_sec,
                "url": self._safe_page_url(),
                "title": await self._safe_page_title(),
            }
            self._progress("webex_page_state_detection_timeout", extra)
            await self._maybe_await(
                self.collect_diagnostics(stage="webex_page_state_detection_timeout", extra=extra)
            )
            return {
                "detected": False,
                "indicators": {},
                "visible_text": "",
                "keyword_hits": [],
                "classification_timeout": True,
                "webclient_frame_exists": False,
                "webclient_frame_ready": False,
                "webclient_frame_detected": False,
                "webclient_frame": None,
                "browser_join_action": None,
                "try_again_action": None,
            }

    async def _download_retry_page_state_from_snapshot(self, timeout_ms=None):
        self._progress("webex_page_state_snapshot_start", {"url": self._safe_page_url()})
        snapshot = await self._page_state_snapshot(timeout_ms=timeout_ms)
        if snapshot.get("detached"):
            return {
                "detected": False,
                "indicators": {},
                "visible_text": "",
                "keyword_hits": [],
                "classification_timeout": False,
                "webclient_frame_exists": False,
                "webclient_frame_ready": False,
                "webclient_frame_detected": False,
                "webclient_frame": None,
                "browser_join_action": None,
                "try_again_action": None,
                "detached": True,
                "retry_rescan": True,
            }
        page_snapshot = snapshot.get("page", {})
        text = str(page_snapshot.get("visible_text") or "")
        hits = {}
        keyword_hits = self._download_retry_keyword_hits(text)
        for group, keywords in self._download_retry_text_groups().items():
            match = self._snapshot_control_match(page_snapshot.get("visible_buttons_links") or [], keywords)
            if group not in hits and match:
                hits[group] = match
        for group, keywords in self._download_retry_text_groups().items():
            if group not in hits and any(keyword in keyword_hits for keyword in keywords):
                hits[group] = "page_text"
        href = str(page_snapshot.get("url") or self._safe_page_url() or "")
        if "/meeting/download" in href.lower():
            hits.setdefault("download_url", "location.href")

        webclient_frame = self._webclient_frame_from_snapshot(snapshot)
        browser_join_action = self._browser_join_action_from_snapshot(snapshot)
        try_again_action = self._try_again_action_from_snapshot(snapshot)
        browser_join_problem_detected = self._browser_join_problem_detected(text, keyword_hits, try_again_action)
        detected = bool(
            hits.get("download_page_indicator")
            or hits.get("installer_download_indicator")
            or hits.get("problem_joining_from_browser")
            or browser_join_problem_detected
        )
        result = {
            "detected": detected,
            "indicators": hits,
            "visible_text": text,
            "hidden_dom_text": str(page_snapshot.get("hidden_text") or "")[:2000],
            "keyword_hits": keyword_hits,
            "url": href,
            "title": str(page_snapshot.get("title") or ""),
            "frame_count": len(snapshot.get("frames") or []),
            "webclient_frame_detected": bool(webclient_frame),
            "webclient_frame_exists": bool(webclient_frame),
            "webclient_frame_ready": bool(webclient_frame and webclient_frame.get("webclient_frame_ready")),
            "webclient_frame": webclient_frame,
            "browser_join_action": browser_join_action,
            "try_again_action": try_again_action,
            "browser_join_problem_detected": browser_join_problem_detected,
        }
        if browser_join_problem_detected:
            self._progress(
                "webex_browser_join_problem_detected",
                {
                    "try_again_action": try_again_action,
                    "webclient_frame_exists": bool(webclient_frame),
                    "webclient_frame_ready": bool(webclient_frame and webclient_frame.get("webclient_frame_ready")),
                },
            )
            if try_again_action:
                self._progress("webex_try_again_action_selected", {"try_again_action": try_again_action})
        self._progress(
            "webex_download_detection_result",
            {
                "detected": result["detected"],
                "indicators": result["indicators"],
                "keyword_hits": result["keyword_hits"],
                "webclient_frame_detected": result["webclient_frame_detected"],
                "webclient_frame_exists": result["webclient_frame_exists"],
                "webclient_frame_ready": result["webclient_frame_ready"],
                "visible_text_input_count": (webclient_frame or {}).get("visible_text_input_count", 0),
                "visible_button_count": (webclient_frame or {}).get("visible_button_count", 0),
                "browser_join_action": browser_join_action,
                "try_again_action": try_again_action,
            },
        )
        return result

    async def _download_retry_page_visible(self, timeout_ms=None):
        return bool((await self._download_retry_page_state(timeout_ms=timeout_ms)).get("detected"))

    def _browser_join_problem_detected(self, text, keyword_hits=None, try_again_action=None):
        normalized = " ".join(str(text or "").split()).lower()
        keyword_hits = set(keyword_hits or [])
        problem = "problem joining from browser?" in normalized or "problem joining from your browser?" in normalized
        try_again = "try again" in normalized or "Retry" in keyword_hits or "Try again" in keyword_hits or bool(try_again_action)
        mobile = "join on mobile" in normalized or "Join on mobile" in keyword_hits
        return bool(problem and try_again and mobile)

    async def _detect_loaded_webex_webclient_frame(self, timeout_ms=None):
        if not self._is_page_available():
            return None
        snapshot = await self._page_state_snapshot(timeout_ms=timeout_ms)
        return self._webclient_frame_from_snapshot(snapshot)

    def _page_state_detection_timeout_sec(self):
        try:
            return max(0.1, float(self.adapter_config().get("page_state_detection_timeout_sec", 3.0)))
        except (TypeError, ValueError):
            return 3.0

    def _frame_snapshot_timeout_sec(self):
        try:
            return max(0.05, float(self.adapter_config().get("frame_snapshot_timeout_sec", 0.5)))
        except (TypeError, ValueError):
            return 0.5

    async def _page_state_snapshot(self, timeout_ms=None):
        pages = self._known_playwright_pages()
        self._progress("webex_context_page_scan_start", {"page_count": len(pages)})
        for index, page in enumerate(pages):
            self._progress(
                "webex_context_page_scan_result",
                {
                    "scope": self._page_scope_name(index),
                    "url": self._safe_scope_url(page),
                    "available": self._is_scope_available(page),
                },
            )
        page_snapshot = await self._snapshot_scope(self.page, "page", timeout_sec=self._frame_snapshot_timeout_sec())
        frames = []
        frame_scopes = self._page_locator_scopes(prefer_meeting_frame=False)[1:]
        self._progress("webex_frame_scan_start", {"frame_count": len(frame_scopes)})
        detached = bool(page_snapshot.get("detached"))
        for scope_name, frame in frame_scopes:
            snapshot = await self._snapshot_scope(frame, scope_name, timeout_sec=self._frame_snapshot_timeout_sec())
            frames.append(snapshot)
            detached = detached or bool(snapshot.get("detached"))
            self._progress(
                "webex_frame_scan_result",
                {
                    "scope": scope_name,
                    "url": snapshot.get("url", ""),
                    "visible_text_input_count": len(snapshot.get("visible_inputs") or []),
                    "visible_button_link_count": len(snapshot.get("visible_buttons_links") or []),
                    "error": snapshot.get("error", ""),
                    "detached": bool(snapshot.get("detached")),
                },
            )
        self._progress("webex_frame_scan_done", {"frame_count": len(frames)})
        snapshot = {"page": page_snapshot, "frames": frames, "detached": detached, "retry_rescan": detached}
        self._progress(
            "webex_page_state_snapshot_done",
            {
                "url": page_snapshot.get("url", ""),
                "title": page_snapshot.get("title", ""),
                "visible_text_chars": len(str(page_snapshot.get("visible_text") or "")),
                "hidden_text_chars": len(str(page_snapshot.get("hidden_text") or "")),
                "frame_count": len(frames),
                "detached": detached,
            },
        )
        return snapshot

    def _known_playwright_pages(self):
        pages = []
        if self.page is not None:
            pages.append(self.page)
        context_pages = []
        context = self.context
        if context is not None:
            try:
                value = getattr(context, "pages", [])
                context_pages = value() if callable(value) else value
            except Exception as exc:
                self._append_browser_log("context_pages_unavailable", repr(exc))
                context_pages = []
        for page in context_pages or []:
            if page is not None and not any(page is existing for existing in pages):
                pages.append(page)
        return pages

    def _page_scope_name(self, index):
        return "page" if index == 0 else f"page[{index}]"

    def _safe_scope_url(self, scope, default=""):
        try:
            return getattr(scope, "url", default) or default
        except Exception:
            return default

    def _is_scope_available(self, scope):
        if scope is None:
            return False
        try:
            is_closed = getattr(scope, "is_closed", None)
            if callable(is_closed):
                return not bool(is_closed())
        except Exception:
            return False
        return True

    async def _snapshot_scope(self, scope, scope_name, timeout_sec=0.5):
        if scope is None or not hasattr(scope, "evaluate"):
            return {"scope": scope_name, "url": "", "title": "", "visible_text": "", "hidden_text": "", "visible_inputs": [], "visible_buttons_links": [], "frame_urls": [], "error": "scope_unavailable"}
        snapshot = {
            "scope": scope_name,
            "url": str(getattr(scope, "url", "") or ""),
            "title": "",
            "visible_text": "",
            "hidden_text": "",
            "visible_inputs": [],
            "visible_buttons_links": [],
            "frame_urls": [],
            "error": "",
            "errors": [],
        }
        if hasattr(scope, "title"):
            try:
                snapshot["title"] = str(await asyncio.wait_for(self._maybe_await(scope.title()), timeout=timeout_sec))
            except Exception as exc:
                if self._is_transient_frame_transition_error(exc):
                    self._mark_snapshot_detached(snapshot, scope_name, "title", exc)
                    return snapshot
                snapshot["title"] = ""
        self._progress("frame_url_seen", {"scope": scope_name, "url": snapshot["url"], "title": snapshot["title"]})

        url_result = await self._snapshot_eval_step(
            scope,
            scope_name,
            "url_title",
            r"""() => ({
              title: document.title || "",
              url: location.href || "",
              frame_urls: Array.from(document.querySelectorAll("iframe, frame")).slice(0, 40).map((el) => el.src || "")
            })""",
            timeout_sec,
        )
        if isinstance(url_result, Mapping):
            if url_result.get("detached"):
                self._mark_snapshot_detached(snapshot, scope_name, "url_title", url_result.get("error", "detached"))
                return snapshot
            snapshot["title"] = str(url_result.get("title") or snapshot["title"] or "")
            snapshot["url"] = str(url_result.get("url") or snapshot["url"] or "")
            snapshot["frame_urls"] = list(url_result.get("frame_urls") or [])

        input_result = await self._snapshot_eval_step(scope, scope_name, "input", self._visible_input_scan_script(), timeout_sec)
        if isinstance(input_result, Mapping) and input_result.get("detached"):
            self._mark_snapshot_detached(snapshot, scope_name, "input", input_result.get("error", "detached"))
            return snapshot
        if isinstance(input_result, list):
            snapshot["visible_inputs"] = input_result
        self._progress(
            "frame_input_probe_result",
            {"scope": scope_name, "url": snapshot["url"], "visible_text_input_count": len(snapshot["visible_inputs"])},
        )

        button_result = await self._snapshot_eval_step(scope, scope_name, "button", self._visible_button_scan_script(), timeout_sec)
        if isinstance(button_result, Mapping) and button_result.get("detached"):
            self._mark_snapshot_detached(snapshot, scope_name, "button", button_result.get("error", "detached"))
            return snapshot
        if isinstance(button_result, list):
            snapshot["visible_buttons_links"] = button_result
        self._progress(
            "frame_button_probe_result",
            {"scope": scope_name, "url": snapshot["url"], "visible_button_link_count": len(snapshot["visible_buttons_links"])},
        )

        text_result = await self._snapshot_eval_step(scope, scope_name, "text", self._visible_text_scan_script(), timeout_sec)
        if isinstance(text_result, Mapping) and text_result.get("detached"):
            self._mark_snapshot_detached(snapshot, scope_name, "text", text_result.get("error", "detached"))
            return snapshot
        if isinstance(text_result, Mapping):
            snapshot["visible_text"] = str(text_result.get("visible_text") or "")
            snapshot["hidden_text"] = str(text_result.get("hidden_text") or "")
        errors = [value for value in snapshot.get("errors", []) if value]
        if errors:
            snapshot["error"] = ",".join(errors)
        return snapshot

    def _visible_input_scan_script(self):
        return r"""
        () => {
          const norm = (value) => String(value || "").replace(/\s+/g, " ").trim();
          const visible = (el) => {
            if (!el || !el.isConnected) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
          };
          const textOf = (el) => norm(el.innerText || el.textContent || el.getAttribute("aria-label") || el.getAttribute("placeholder") || "");
          const summarize = (el) => ({
            tag: el.tagName || "",
            role: el.getAttribute("role") || "",
            type: el.getAttribute("type") || "",
            name: el.getAttribute("name") || "",
            id: el.getAttribute("id") || "",
            text: textOf(el).slice(0, 300),
            aria_label: el.getAttribute("aria-label") || "",
            placeholder: el.getAttribute("placeholder") || "",
            href: el.getAttribute("href") || "",
          });
          return Array.from(document.querySelectorAll(
            'mdc-input input:not([type="hidden"]), mdc-input textarea, input:not([type="hidden"]), textarea, [role="textbox"], [contenteditable="true"]'
          )).filter(visible).slice(0, 30).map(summarize);
        }
        """

    def _visible_button_scan_script(self):
        return r"""
        () => {
          const norm = (value) => String(value || "").replace(/\s+/g, " ").trim();
          const visible = (el) => {
            if (!el || !el.isConnected) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
          };
          const textOf = (el) => norm(el.innerText || el.textContent || el.getAttribute("aria-label") || el.getAttribute("placeholder") || "");
          const summarize = (el) => ({
            tag: el.tagName || "",
            role: el.getAttribute("role") || "",
            type: el.getAttribute("type") || "",
            name: el.getAttribute("name") || "",
            id: el.getAttribute("id") || "",
            text: textOf(el).slice(0, 300),
            aria_label: el.getAttribute("aria-label") || "",
            placeholder: el.getAttribute("placeholder") || "",
            href: el.getAttribute("href") || "",
          });
          return Array.from(document.querySelectorAll("button, a, [role='button'], [role='link']"))
            .filter(visible).slice(0, 80).map(summarize);
        }
        """

    def _visible_text_scan_script(self):
        return r"""
        () => {
          const norm = (value) => String(value || "").replace(/\s+/g, " ").trim();
          const visibleText = norm(document.body && document.body.innerText || "");
          const hiddenText = norm(document.body && document.body.textContent || "");
          return {
            visible_text: visibleText.slice(0, 6000),
            hidden_text: hiddenText.slice(0, 6000),
          };
        }
        """

    async def _snapshot_eval_step(self, scope, scope_name, step, script, timeout_sec):
        try:
            return await asyncio.wait_for(self._maybe_await(scope.evaluate(script)), timeout=timeout_sec)
        except asyncio.TimeoutError:
            self._progress("frame_snapshot_timeout", {"scope": scope_name, "step": step})
            return None
        except Exception as exc:
            if self._is_transient_frame_transition_error(exc):
                self._progress(
                    "webex_frame_detached_during_transition",
                    {"scope": scope_name, "step": step, "error": repr(exc), "retry_rescan": True},
                )
                return {"detached": True, "retry_rescan": True, "error": repr(exc)}
            self._append_browser_log("frame_snapshot_step_failed", f"{scope_name}:{step}:{exc!r}")
            return None

    def _mark_snapshot_detached(self, snapshot, scope_name, step, exc):
        snapshot["detached"] = True
        snapshot["retry_rescan"] = True
        snapshot["error"] = "detached"
        snapshot.setdefault("errors", []).append("detached")
        self._progress(
            "webex_frame_detached_during_transition",
            {"scope": scope_name, "step": step, "error": repr(exc), "retry_rescan": True},
        )

    def _webclient_frame_from_snapshot(self, snapshot):
        best = None
        frame_scopes = self._page_locator_scopes(prefer_meeting_frame=False)[1:]
        for index, details in enumerate(snapshot.get("frames") or []):
            url = str(details.get("url") or "")
            name = str(details.get("name") or details.get("scope") or "")
            url_lower = url.lower()
            outer_download_shell = "/meeting/download" in url_lower
            url_score = self._webex_meeting_frame_url_score(url, name)
            visible_inputs = details.get("visible_inputs") or []
            visible_buttons = details.get("visible_buttons_links") or []
            visible_text = str(details.get("visible_text") or "").lower()
            join_button = any(
                self._snapshot_control_text_matches(item, ("join", "start meeting", "참여", "참가"))
                for item in visible_buttons
                if isinstance(item, Mapping)
            )
            continue_button = any(
                self._snapshot_control_text_matches(item, ("next", "continue", "계속"))
                for item in visible_buttons
                if isinstance(item, Mapping)
            )
            prejoin_text = any(
                token in visible_text
                for token in ("join", "next", "continue", "name", "meeting", "참여", "참가", "이름", "계속")
            )
            prejoin_control = any(
                self._snapshot_control_text_matches(item, ("mute", "camera", "audio", "microphone", "음소거", "카메라", "오디오"))
                for item in visible_buttons
                if isinstance(item, Mapping)
            )
            ready = bool(not outer_download_shell and (visible_inputs or join_button or continue_button or prejoin_control))
            score = url_score + (10 if visible_inputs else 0) + (6 if join_button else 0) + (3 if prejoin_text else 0)
            detected = bool(not outer_download_shell and (url_score >= 10 or (url_score > 0 and (visible_inputs or join_button or prejoin_text))))
            if not detected:
                continue
            candidate = {
                "scope": details.get("scope", f"frame[{index}]"),
                "url": url,
                "name": name,
                "visible_text_input_count": len(visible_inputs),
                "visible_button_count": len(visible_buttons),
                "join_button_detected": join_button,
                "continue_button_detected": continue_button,
                "prejoin_control_detected": prejoin_control,
                "prejoin_text": prejoin_text,
                "webclient_frame_exists": True,
                "webclient_frame_ready": ready,
                "score": score,
            }
            self._progress("webex_webclient_frame_candidate", candidate)
            if best is None or candidate["score"] > best["details"]["score"]:
                best = {"index": index, "details": candidate}
        if not best:
            return None
        if best["details"].get("webclient_frame_ready") and 0 <= best["index"] < len(frame_scopes):
            self._preferred_webex_meeting_frame = frame_scopes[best["index"]][1]
        return best["details"]

    def _browser_join_action_from_snapshot(self, snapshot):
        scopes = [snapshot.get("page") or {}] + list(snapshot.get("frames") or [])
        best = None
        for index, details in enumerate(scopes):
            visible_buttons = details.get("visible_buttons_links") or []
            if not visible_buttons:
                continue
            match = self._snapshot_browser_join_control_match(visible_buttons)
            if not match:
                continue
            scope = details.get("scope") or ("page" if index == 0 else f"frame[{index - 1}]")
            candidate = {
                "scope": scope,
                "url": str(details.get("url") or ""),
                "visible_button_count": len(visible_buttons),
                "selector": match.get("selector"),
                "text": match.get("text"),
                "score": len(visible_buttons) + match.get("score", 0),
            }
            self._progress("browser_join_candidate_found", candidate)
            if best is None or candidate["score"] > best["score"]:
                best = candidate
        return best

    def _snapshot_browser_join_control_match(self, items):
        phrases = (
            ("join from your browser", 40),
            ("join from browser", 40),
            ("join from this browser", 40),
            ("continue in browser", 38),
            ("continue in this browser", 38),
            ("open in browser", 36),
            ("use web app", 34),
            ("use browser", 34),
            ("join meeting", 24),
            ("join", 12),
            ("브라우저에서 참여", 40),
            ("브라우저에서 참가", 40),
            ("브라우저로 참여", 36),
            ("브라우저로 참가", 36),
            ("브라우저에서 계속", 38),
            ("이 브라우저에서 참여", 40),
            ("이 브라우저에서 참가", 40),
            ("웹에서 참여", 34),
            ("웹에서 참가", 34),
        )
        best = None
        for item in items:
            if not isinstance(item, Mapping):
                continue
            text = " ".join(
                str(item.get(key) or "")
                for key in ("text", "aria_label", "placeholder", "name", "id")
            ).strip()
            compact = " ".join(text.split()).lower()
            if any(forbidden in compact for forbidden in ("get ready to join", "problem joining", "join on mobile", "download")):
                continue
            lowered = text.lower()
            for phrase, score in phrases:
                phrase_lower = phrase.lower()
                if phrase_lower in {"join", "join meeting"}:
                    visible_label = " ".join(
                        str(item.get(key) or "")
                        for key in ("text", "aria_label")
                    ).strip().lower()
                    element_id = str(item.get("id") or "").strip().lower()
                    if visible_label not in {phrase_lower, "join meeting", "join"} and element_id not in {"browser", "join-from-browser"}:
                        continue
                elif phrase_lower not in lowered:
                    continue
                element_id = str(item.get("id") or "")
                selector = f"#{element_id}" if element_id else None
                candidate = {"selector": selector, "text": text[:120], "score": score}
                if best is None or candidate["score"] > best["score"]:
                    best = candidate
        return best

    def _try_again_action_from_snapshot(self, snapshot):
        scopes = [snapshot.get("page") or {}] + list(snapshot.get("frames") or [])
        for index, details in enumerate(scopes):
            match = self._snapshot_control_match(details.get("visible_buttons_links") or [], ("try again", "retry", "다시 시도"))
            if not match:
                continue
            scope = details.get("scope") or ("page" if index == 0 else f"frame[{index - 1}]")
            return {"scope": scope, "selector": match, "url": str(details.get("url") or "")}
        return None

    def _snapshot_control_text_matches(self, item, needles):
        text = " ".join(
            str(item.get(key) or "")
            for key in ("text", "aria_label", "placeholder", "name", "id")
        ).lower()
        return any(str(needle).lower() in text for needle in needles)

    def _snapshot_control_match(self, items, needles):
        for item in items:
            if not isinstance(item, Mapping):
                continue
            if not self._snapshot_control_text_matches(item, needles):
                continue
            element_id = str(item.get("id") or "")
            if element_id:
                return f"#{element_id}"
            text = str(item.get("text") or item.get("aria_label") or "").strip()
            return f"visible_control:{text[:80]}" if text else "visible_control"
        return None

    def _webex_meeting_frame_url_score(self, url, name=""):
        text = f"{url} {name}".lower()
        if "unified-webclient-iframe" in text:
            return 8
        if "web.webex.com/guest-join-meeting" in text:
            return 12
        if "web.webex.com/meeting" in text:
            return 10
        if "web.webex.com" in text and "join" in text:
            return 11
        if "/meeting" in text:
            return 5
        return 0

    def _is_strong_webex_guest_join_frame_url(self, url):
        return self._webex_meeting_frame_url_score(str(url or "")) >= 10

    def _select_live_webclient_frame_candidate(self):
        best = None
        for scope_name, frame in self._page_locator_scopes(prefer_meeting_frame=False)[1:]:
            url = str(getattr(frame, "url", "") or "")
            name = self._safe_frame_name(frame)
            score = self._webex_meeting_frame_url_score(url, name)
            if score <= 0:
                continue
            candidate = {"scope": scope_name, "url": url, "name": name, "score": score}
            self._progress("webex_webclient_frame_candidate", candidate)
            if best is None or score > best["score"]:
                best = {**candidate, "frame": frame}
        if best:
            self._preferred_webex_meeting_frame = best["frame"]
        return best

    def _safe_frame_name(self, frame):
        try:
            name = getattr(frame, "name", "")
            return name() if callable(name) else str(name or "")
        except Exception:
            return ""

    async def _visible_locator_count(self, scope, selector, timeout_ms, limit=10):
        try:
            locators = scope.locator(selector)
            count = await self._maybe_await(locators.count()) if hasattr(locators, "count") else 1
        except Exception:
            return 0
        visible = 0
        for index in range(min(int(count or 0), int(limit))):
            locator = locators.nth(index) if hasattr(locators, "nth") else locators.first
            try:
                await locator.wait_for(state="visible", timeout=timeout_ms)
                visible += 1
            except Exception:
                continue
        return visible

    async def _frame_contains_prejoin_text(self, frame):
        if not hasattr(frame, "evaluate"):
            return False
        try:
            text = await self._maybe_await(
                frame.evaluate(
                    r"""() => String(document.body && (document.body.innerText || document.body.textContent) || '')
                        .replace(/\s+/g, ' ')
                        .slice(0, 2000)"""
                )
            )
        except Exception:
            return False
        lowered = str(text or "").lower()
        return any(
            token in lowered
            for token in (
                "join",
                "next",
                "continue",
                "name",
                "meeting",
                "참여",
                "참가",
                "이름",
                "계속",
            )
        )

    async def _browser_join_after_retry_seen(self, timeout_ms=None):
        for group in (
            "join_from_browser",
            "join_from_this_browser",
            "continue_in_browser",
            "use_web_app",
            "open_in_browser",
            "display_name",
            "name_input",
        ):
            if await self._first_visible_selector(group, timeout_ms=timeout_ms):
                return True
        return False

    async def _click_try_again_browser_join(self, timeout_ms=None):
        self._progress("webex_try_again_click_attempt")
        timeout = self._download_retry_action_timeout_ms(timeout_ms)
        return await self._click_text_action(
            ["Try again", "Retry", "다시 시도"],
            forbidden_texts=self._download_retry_forbidden_texts(),
            stage="webex_try_again",
            exact=True,
            frames=True,
            js_first=True,
            deadline_sec=self._download_retry_click_total_timeout_sec(),
            allow_js_fallback=True,
            allow_coordinate_fallback=True,
            timeout_ms=timeout,
        )

    async def _click_try_again_from_state(self, state, timeout_ms=None):
        click_total_timeout = self._download_retry_click_total_timeout_sec()
        click_deadline = asyncio.get_running_loop().time() + click_total_timeout
        got_it = False
        try:
            remaining = max(0.01, click_deadline - asyncio.get_running_loop().time())
            got_it = await asyncio.wait_for(
                self._click_got_it_button(timeout_ms=timeout_ms),
                timeout=remaining,
            )
            action = state.get("try_again_action") or {}
            direct_clicked = None
            if self._try_again_direct_candidate_allowed(action):
                remaining = max(0.01, click_deadline - asyncio.get_running_loop().time())
                direct_clicked = await asyncio.wait_for(
                    self._click_try_again_candidate(action, timeout_ms=timeout_ms),
                    timeout=remaining,
                )
            if direct_clicked:
                return got_it, direct_clicked
            remaining = max(0.01, click_deadline - asyncio.get_running_loop().time())
            clicked = await asyncio.wait_for(
                self._click_try_again_browser_join(timeout_ms=timeout_ms),
                timeout=remaining,
            )
            if clicked:
                self._progress("webex_try_again_click_success", clicked)
            else:
                self._progress("webex_try_again_click_failed", {"try_again_action": action})
            return got_it, clicked
        except asyncio.TimeoutError:
            self._progress(
                "webex_try_again_click_timeout",
                {"timeout_sec": click_total_timeout, "try_again_action": state.get("try_again_action")},
            )
            await self._raise_download_retry_try_again_not_clickable(
                {**state, "got_it_clicked": got_it, "click_total_timeout_sec": click_total_timeout, "click_timeout": True}
            )

    def _try_again_direct_candidate_allowed(self, action):
        return bool(
            isinstance(action, Mapping)
            and action.get("selector") == "#fallBkJoinByBrowser"
            and action.get("scope") in {"page", "frame[0]"}
        )

    async def _click_try_again_candidate(self, action, timeout_ms=None):
        selector = action.get("selector")
        scope_name = action.get("scope")
        timeout = self._download_retry_action_timeout_ms(timeout_ms)
        attempt = {"selector": selector, "scope": scope_name, "method": "candidate", "timeout_ms": timeout}
        self._progress("webex_try_again_click_attempt", attempt)
        scopes = self._page_locator_scopes(prefer_meeting_frame=False)
        scope = next((candidate_scope for candidate_name, candidate_scope in scopes if candidate_name == scope_name), None)
        if scope is None:
            self._progress("webex_try_again_click_failed", {**attempt, "reason": "scope_not_found"})
            return None

        clicked = await self._js_click_try_again_candidate(scope_name, scope, selector, timeout)
        if clicked:
            self._progress("webex_try_again_click_success", clicked)
            return clicked

        locator = scope.locator(selector).first
        for method, kwargs in (
            ("playwright_locator_click", {"timeout": timeout}),
            ("playwright_force_click", {"timeout": timeout, "force": True}),
        ):
            try:
                await locator.click(**kwargs)
                clicked = {"ok": True, "selector": selector, "scope": scope_name, "method": method}
                self._progress("webex_try_again_click_success", clicked)
                return clicked
            except PlaywrightTimeoutError as exc:
                self._progress("webex_try_again_click_failed", {**attempt, "method": method, "reason": "timeout", "error": repr(exc)})
            except self._safe_playwright_errors() as exc:
                self._last_page_metadata_error = exc
                self._append_browser_log("webex_try_again_candidate_click_unavailable", repr(exc))
                self._progress("webex_try_again_click_failed", {**attempt, "method": method, "reason": "playwright_error", "error": repr(exc)})
            except Exception as exc:
                self._append_browser_log("webex_try_again_candidate_click_unavailable", repr(exc))
                self._progress("webex_try_again_click_failed", {**attempt, "method": method, "reason": "error", "error": repr(exc)})

        try:
            box = await asyncio.wait_for(self._maybe_await(locator.bounding_box()), timeout=timeout / 1000)
            if box:
                x = float(box.get("x", 0)) + float(box.get("width", 0)) / 2
                y = float(box.get("y", 0)) + float(box.get("height", 0)) / 2
                mouse = getattr(scope, "mouse", None) or getattr(self.page, "mouse", None)
                if mouse and hasattr(mouse, "click"):
                    await asyncio.wait_for(self._maybe_await(mouse.click(x, y)), timeout=timeout / 1000)
                    clicked = {"ok": True, "selector": selector, "scope": scope_name, "method": "coordinate_click", "x": x, "y": y}
                    self._progress("webex_try_again_click_success", clicked)
                    return clicked
        except Exception as exc:
            self._append_browser_log("webex_try_again_candidate_coordinate_click_unavailable", repr(exc))
        self._progress("webex_try_again_click_failed", {**attempt, "method": "coordinate_click", "reason": "no_clickable_box"})
        return None

    async def _js_click_try_again_candidate(self, scope_name, scope, selector, timeout_ms):
        script = r"""
        (selector) => {
          const visible = (el) => {
            if (!el || !el.isConnected) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
          };
          const norm = (value) => String(value || "").replace(/\s+/g, " ").trim();
          const target = document.querySelector(selector);
          if (!target) return {ok: false, method: "js_candidate_click", reason: "selector_not_found"};
          if (!visible(target)) return {ok: false, method: "js_candidate_click", reason: "not_visible"};
          target.scrollIntoView({block: "center", inline: "center"});
          target.click();
          const rect = target.getBoundingClientRect();
          return {
            ok: true,
            method: "js_candidate_click",
            selector,
            clicked_tag: target.tagName || "",
            clicked_text: norm(target.innerText || target.textContent || target.getAttribute("aria-label") || "").slice(0, 160),
            x: rect.left + rect.width / 2,
            y: rect.top + rect.height / 2
          };
        }
        """
        try:
            result = await asyncio.wait_for(self._maybe_await(scope.evaluate(script, selector)), timeout=timeout_ms / 1000)
        except asyncio.TimeoutError as exc:
            self._progress(
                "webex_try_again_click_timeout",
                {"selector": selector, "scope": scope_name, "method": "js_candidate_click", "error": repr(exc)},
            )
            return None
        except Exception as exc:
            self._append_browser_log("webex_try_again_candidate_js_click_unavailable", repr(exc))
            self._progress(
                "webex_try_again_click_failed",
                {"selector": selector, "scope": scope_name, "method": "js_candidate_click", "reason": "error", "error": repr(exc)},
            )
            return None
        if isinstance(result, Mapping) and result.get("ok"):
            return {**result, "selector": selector, "scope": scope_name, "method": result.get("method") or "js_candidate_click"}
        self._progress(
            "webex_try_again_click_failed",
            {
                "selector": selector,
                "scope": scope_name,
                "method": "js_candidate_click",
                "reason": (result or {}).get("reason") if isinstance(result, Mapping) else "not_clicked",
            },
        )
        return None

    async def _after_try_again_click_rescan(self, timeout_ms=None):
        await self._dismiss_external_protocol_prompt(stage="after_try_again_click")
        self._discard_browser_join_snapshot_state()
        self._progress("webex_post_try_again_rescan_start", {"page_count": len(self._known_playwright_pages())})
        result = await self._download_retry_page_state(timeout_ms=timeout_ms)
        self._progress(
            "webex_post_try_again_rescan_result",
            {
                "detected": result.get("detected"),
                "webclient_frame_exists": result.get("webclient_frame_exists"),
                "webclient_frame_ready": result.get("webclient_frame_ready"),
                "browser_join_action": result.get("browser_join_action"),
                "try_again_action": result.get("try_again_action"),
            },
        )
        return result

    async def _click_got_it_button(self, timeout_ms=None):
        self._progress("webex_got_it_click_attempt")
        clicked = await self._safe_optional_got_it_click(timeout_ms=timeout_ms)
        if clicked:
            self._progress("webex_got_it_clicked", clicked)
        else:
            self._progress("webex_got_it_click_skipped")
        return clicked

    def _download_retry_action_timeout_ms(self, timeout_ms=None):
        configured = self.timeout_ms("download_retry_action_timeout_ms", 500)
        values = [configured]
        if timeout_ms is not None:
            values.append(timeout_ms)
        return max(1, min(500, *[int(value) for value in values if value is not None]))

    def _download_retry_click_total_timeout_sec(self):
        try:
            return max(0.01, float(self.adapter_config().get("download_retry_click_total_timeout_sec", 5.0)))
        except (TypeError, ValueError):
            return 5.0

    async def _safe_optional_got_it_click(self, timeout_ms=None):
        if not self._is_page_available():
            return False
        timeout = self._download_retry_action_timeout_ms(timeout_ms)
        texts = ["Got it", "확인", "알겠습니다"]
        clicked = await self._js_text_action_click(texts, exact=True, frames=True)
        if clicked:
            return clicked
        for text in texts:
            if self._deadline_expired(None):
                return False
            for selector, locator_factory in self._text_action_locator_factories(text, exact=True)[:4]:
                for scope_name, scope in self._text_action_scopes(frames=True)[:2]:
                    try:
                        locator = locator_factory(scope).first
                        await locator.wait_for(state="visible", timeout=timeout)
                        details = await self._text_action_locator_details(locator, selector, text)
                        if self._text_action_rejected_reason(details, texts, [], exact=True):
                            continue
                        if not await self._text_action_locator_enabled(locator, timeout=timeout):
                            continue
                        try:
                            await locator.click(timeout=timeout)
                        except TypeError:
                            await locator.click()
                        return {
                            "ok": True,
                            "method": "optional_locator_text_click",
                            "text": text,
                            "selector": selector,
                            "scope": scope_name,
                            **details,
                        }
                    except PlaywrightTimeoutError:
                        continue
                    except self._safe_playwright_errors() as exc:
                        self._last_page_metadata_error = exc
                        self._append_browser_log("webex_got_it_optional_click_unavailable", repr(exc))
                        continue
                    except Exception as exc:
                        self._append_browser_log("webex_got_it_optional_click_unavailable", repr(exc))
                        continue
        return False

    async def _raise_download_retry_try_again_not_clickable(self, state):
        text = await self._visible_text_excerpt()
        extra = {
            "download_retry": state,
            "download_retry_keyword_hits": self._download_retry_keyword_hits(text),
            "visible_text": text,
            "html_snippet": await self._download_retry_html_snippet(),
            "url": self._safe_page_url(),
            "title": await self._safe_page_title(),
            "links_buttons_debug": await self._links_buttons_debug_info(),
            "text_action_candidates": await self._text_action_diagnostics(
                ["Got it", "확인", "알겠습니다", "Try again", "Retry", "다시 시도"],
                forbidden_texts=self._download_retry_forbidden_texts(),
            ),
            "input_debug": await self._input_debug_info(),
        }
        self._progress("webex_download_retry_try_again_not_clickable", extra)
        diagnostics = await self._maybe_await(
            self.collect_diagnostics(stage="webex_download_retry_try_again_not_clickable", extra=extra)
        )
        raise RuntimeError(
            "Webex download/retry page Try again was not clickable. "
            f"Visible text: {text}. Diagnostics: {diagnostics}"
        )

    async def _click_text_action(
        self,
        texts,
        forbidden_texts=None,
        stage=None,
        exact=True,
        frames=True,
        allow_js_fallback=True,
        allow_coordinate_fallback=True,
        js_first=False,
        deadline_sec=None,
        timeout_ms=None,
    ):
        timeout = self.timeout_ms("optional_selector_timeout_ms", 1000) if timeout_ms is None else timeout_ms
        forbidden_texts = list(forbidden_texts or [])
        safe_texts = [str(text) for text in texts if str(text or "").strip()]
        if not safe_texts or not self._is_page_available():
            return None
        deadline = None
        if deadline_sec is not None:
            deadline = asyncio.get_running_loop().time() + max(0.05, float(deadline_sec))

        if js_first and allow_js_fallback:
            clicked = await self._js_text_action_click(
                safe_texts,
                forbidden_texts=forbidden_texts,
                exact=exact,
                frames=frames,
            )
            if clicked:
                self._progress(f"{stage or 'text_action'}_clicked", clicked)
                return clicked

        for text in safe_texts:
            for selector, locator_factory in self._text_action_locator_factories(text, exact=exact):
                for scope_name, scope in self._text_action_scopes(frames=frames):
                    if self._deadline_expired(deadline):
                        return None
                    try:
                        locator = locator_factory(scope).first
                        await locator.wait_for(state="visible", timeout=timeout)
                        details = await self._text_action_locator_details(locator, selector, text)
                        if self._text_action_rejected_reason(details, safe_texts, forbidden_texts, exact=exact):
                            continue
                        if not await self._text_action_locator_enabled(locator, timeout=timeout):
                            continue
                        result = {
                            "ok": True,
                            "method": "locator_text_click",
                            "text": text,
                            "selector": selector,
                            "scope": scope_name,
                            **details,
                        }
                        try:
                            await locator.click(timeout=timeout)
                        except TypeError:
                            await locator.click(force=True)
                            result["method"] = "locator_force_text_click"
                        except self._safe_playwright_errors():
                            await locator.click(force=True, timeout=timeout)
                            result["method"] = "locator_force_text_click"
                        self._progress(f"{stage or 'text_action'}_clicked", result)
                        return result
                    except PlaywrightTimeoutError:
                        continue
                    except self._safe_playwright_errors() as exc:
                        self._last_page_metadata_error = exc
                        self._append_browser_log("text_action_locator_unavailable", repr(exc))
                        continue
                    except Exception as exc:
                        self._append_browser_log("text_action_locator_unavailable", repr(exc))
                        continue

        if allow_js_fallback and not js_first and not self._deadline_expired(deadline):
            clicked = await self._js_text_action_click(
                safe_texts,
                forbidden_texts=forbidden_texts,
                exact=exact,
                frames=frames,
            )
            if clicked:
                self._progress(f"{stage or 'text_action'}_clicked", clicked)
                return clicked

        if (
            allow_coordinate_fallback
            and self._coordinate_text_fallback_allowed(safe_texts)
            and not self._deadline_expired(deadline)
        ):
            clicked = await self._coordinate_text_action_click(
                safe_texts,
                forbidden_texts=forbidden_texts,
                exact=exact,
                frames=frames,
                timeout_ms=timeout,
            )
            if clicked:
                self._progress(f"{stage or 'text_action'}_clicked", clicked)
                return clicked
        return None

    def _deadline_expired(self, deadline):
        return deadline is not None and asyncio.get_running_loop().time() >= deadline

    def _text_action_scopes(self, frames=True):
        scopes = self._page_locator_scopes()
        return scopes if frames else scopes[:1]

    def _text_action_locator_factories(self, text, exact=True):
        escaped = self._css_string(text)
        regex = re.compile(rf"^\s*{re.escape(text)}\s*$" if exact else re.escape(text), re.IGNORECASE)
        factories = []
        factories.append((f"role=button[name=/{re.escape(text)}/i]", lambda scope: scope.get_by_role("button", name=regex)))
        factories.append((f"role=link[name=/{re.escape(text)}/i]", lambda scope: scope.get_by_role("link", name=regex)))
        factories.append((f'get_by_text("{text}", exact={exact})', lambda scope: scope.get_by_text(text, exact=exact)))
        factories.append((f"text={text}", lambda scope: scope.locator(f"text={text}")))
        factories.append((f":text({escaped})", lambda scope: scope.locator(f":text({escaped})")))
        for prefix in ("button", "a", '[role="button"]', '[role="link"]', "div", "span"):
            selector = f"{prefix}:has-text({escaped})"
            factories.append((selector, lambda scope, selector=selector: scope.locator(selector)))
        return factories

    def _css_string(self, text):
        return json.dumps(str(text), ensure_ascii=False)

    async def _text_action_locator_enabled(self, locator, timeout=1000):
        if not hasattr(locator, "is_enabled"):
            return True
        try:
            return bool(await self._maybe_await(locator.is_enabled(timeout=timeout)))
        except TypeError:
            return bool(await self._maybe_await(locator.is_enabled()))
        except Exception:
            return True

    async def _text_action_locator_details(self, locator, selector, desired_text):
        details = {
            "candidate_text": self._selector_text_hint(selector) or desired_text,
            "clickable_target_text": "",
            "broad_context_text": "",
            "tag": "",
            "role": "",
            "href": "",
            "onclick": False,
            "tabindex": "",
            "rect": None,
            "container_text": "",
        }
        if hasattr(locator, "evaluate"):
            script = r"""
            (el) => {
              const norm = (value) => String(value || "").replace(/\s+/g, " ").trim();
              const text = norm(el.innerText || el.textContent || el.getAttribute("aria-label") || "");
              const target = el.closest("button, a, [role='button'], [role='link'], [onclick], [tabindex]") || el;
              const context = el.closest("main, section, article, body") || el;
              const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
              return {
                candidate_text: text,
                clickable_target_text: norm(target.innerText || target.textContent || target.getAttribute("aria-label") || ""),
                broad_context_text: norm(context.innerText || context.textContent || context.getAttribute("aria-label") || ""),
                container_text: norm(target.innerText || target.textContent || target.getAttribute("aria-label") || ""),
                tag: el.tagName || "",
                role: el.getAttribute("role") || "",
                href: el.getAttribute("href") || "",
                onclick: !!el.onclick || el.hasAttribute("onclick"),
                tabindex: el.getAttribute("tabindex") || "",
                rect: rect ? {x: rect.x, y: rect.y, width: rect.width, height: rect.height} : null
              };
            }
            """
            try:
                result = await self._maybe_await(locator.evaluate(script))
                if isinstance(result, Mapping):
                    details.update(result)
            except Exception:
                pass
        return details

    def _selector_text_hint(self, selector):
        selector = str(selector or "")
        for pattern in (r'has-text\((".*?")\)', r':text\((".*?")\)', r'^text="?([^"]+)"?$'):
            match = re.search(pattern, selector)
            if not match:
                continue
            value = match.group(1)
            if value.startswith('"'):
                try:
                    return json.loads(value)
                except Exception:
                    return value.strip('"')
            return value
        return ""

    def _text_action_rejected_reason(self, details, desired_texts, forbidden_texts, exact=True):
        text = " ".join(str(details.get("candidate_text") or "").split())
        target = " ".join(
            str(details.get("clickable_target_text") or details.get("container_text") or "").split()
        )
        if not text:
            return "empty_text"
        if exact and not any(text.lower() == desired.lower() for desired in desired_texts):
            return "desired_text_not_exact"
        if not exact and not any(desired.lower() in text.lower() for desired in desired_texts):
            return "desired_text_missing"
        for forbidden in forbidden_texts:
            forbidden_lower = str(forbidden).lower()
            if forbidden_lower and (forbidden_lower in text.lower() or forbidden_lower in target.lower()):
                return f"forbidden_text:{forbidden}"
        return None

    async def _js_text_action_click(self, texts, forbidden_texts=None, exact=True, frames=True):
        for scope_name, scope in self._text_action_scopes(frames=frames):
            if not hasattr(scope, "evaluate"):
                continue
            try:
                result = await self._maybe_await(
                    scope.evaluate(self._text_action_js_script(click=True), {
                        "texts": list(texts),
                        "forbiddenTexts": list(forbidden_texts or []),
                        "exact": bool(exact),
                    })
                )
            except TypeError:
                try:
                    result = await self._maybe_await(scope.evaluate(self._text_action_js_script(click=True)))
                except Exception as exc:
                    self._append_browser_log("webex_text_action_js_click_failed", repr(exc))
                    continue
            except self._safe_playwright_errors() as exc:
                self._last_page_metadata_error = exc
                self._append_browser_log("webex_text_action_js_click_failed", repr(exc))
                continue
            except Exception as exc:
                self._append_browser_log("webex_text_action_js_click_failed", repr(exc))
                continue
            if isinstance(result, Mapping) and result.get("ok"):
                return {**dict(result), "scope": scope_name}
        return None

    async def _coordinate_text_action_click(self, texts, forbidden_texts=None, exact=True, frames=True, timeout_ms=1000):
        for text in texts:
            for selector, locator_factory in self._text_action_locator_factories(text, exact=exact):
                for scope_name, scope in self._text_action_scopes(frames=frames):
                    try:
                        locator = locator_factory(scope).first
                        await locator.wait_for(state="visible", timeout=timeout_ms)
                        details = await self._text_action_locator_details(locator, selector, text)
                        if self._text_action_rejected_reason(details, texts, forbidden_texts or [], exact=exact):
                            continue
                        box = None
                        if hasattr(locator, "bounding_box"):
                            box = await self._maybe_await(locator.bounding_box())
                        box = box or details.get("rect")
                        if not box or float(box.get("width", 0)) <= 0 or float(box.get("height", 0)) <= 0:
                            continue
                        mouse = getattr(self.page, "mouse", None)
                        if not mouse or not hasattr(mouse, "click"):
                            continue
                        x = float(box.get("x", 0)) + (float(box.get("width", 0)) / 2)
                        y = float(box.get("y", 0)) + (float(box.get("height", 0)) / 2)
                        await self._maybe_await(mouse.click(x, y))
                        return {
                            "ok": True,
                            "method": "coordinate_text_click",
                            "text": text,
                            "selector": selector,
                            "scope": scope_name,
                            "x": x,
                            "y": y,
                            **details,
                        }
                    except PlaywrightTimeoutError:
                        continue
                    except Exception as exc:
                        self._append_browser_log("webex_text_action_coordinate_click_failed", repr(exc))
                        continue
        return None

    def _coordinate_text_fallback_allowed(self, texts):
        allowed = {"got it", "try again", "retry", "다시 시도", "확인", "알겠습니다"}
        return all(str(text).strip().lower() in allowed for text in texts)

    async def _text_action_diagnostics(self, texts, forbidden_texts=None, frames=True):
        results = []
        for scope_name, scope in self._text_action_scopes(frames=frames):
            if not hasattr(scope, "evaluate"):
                continue
            try:
                result = await self._maybe_await(
                    scope.evaluate(self._text_action_js_script(click=False), {
                        "texts": list(texts),
                        "forbiddenTexts": list(forbidden_texts or []),
                        "exact": False,
                    })
                )
                if isinstance(result, list):
                    for item in result:
                        if isinstance(item, Mapping):
                            results.append({**dict(item), "scope": scope_name})
            except TypeError:
                continue
            except Exception as exc:
                self._append_browser_log("webex_text_action_diagnostics_failed", repr(exc))
                continue
        return results[:120]

    def _text_action_js_script(self, click=False):
        action = "true" if click else "false"
        return rf"""
        (payload) => {{
          const texts = (payload && payload.texts) || [];
          const forbiddenTexts = (payload && payload.forbiddenTexts) || [];
          const exact = payload ? !!payload.exact : true;
          const shouldClick = {action};
          const norm = (value) => String(value || "").replace(/\s+/g, " ").trim();
          const lower = (value) => norm(value).toLowerCase();
          const desiredMatch = (text) => exact
            ? texts.some((item) => lower(text) === lower(item))
            : texts.some((item) => lower(text).includes(lower(item)));
          const forbiddenHit = (text) => forbiddenTexts.find((item) => item && lower(text).includes(lower(item)));
          const isVisible = (el) => {{
            if (!el || !el.isConnected) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
          }};
          const rectInfo = (el) => {{
            const rect = el.getBoundingClientRect();
            return {{x: rect.x, y: rect.y, width: rect.width, height: rect.height}};
          }};
          const tagScore = (el) => {{
            const tag = (el.tagName || "").toLowerCase();
            const role = lower(el.getAttribute("role"));
            if (tag === "button") return 0;
            if (tag === "a") return 1;
            if (role === "button") return 2;
            if (role === "link") return 3;
            if (el.onclick || el.hasAttribute("onclick")) return 4;
            if (el.hasAttribute("tabindex")) return 5;
            return 20;
          }};
          const clickableParent = (el) => el.closest("button, a, [role='button'], [role='link'], [onclick], [tabindex]") || el;
          const visit = (root, out) => {{
            const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
            let node = root.nodeType === Node.ELEMENT_NODE ? root : walker.nextNode();
            while (node) {{
              out.push(node);
              if (node.shadowRoot) visit(node.shadowRoot, out);
              node = walker.nextNode();
            }}
          }};
          const elements = [];
          visit(document.documentElement || document.body, elements);
          const candidates = [];
          for (const el of elements) {{
            const text = norm(el.innerText || el.textContent || el.getAttribute("aria-label") || "");
            if (!text || !desiredMatch(text)) continue;
            const target = clickableParent(el);
            const targetText = norm(target.innerText || target.textContent || target.getAttribute("aria-label") || text);
            const forbidden = forbiddenHit(`${{text}} ${{targetText}}`);
            const item = {{
              ok: false,
              method: shouldClick ? "js_text_click" : "js_text_diagnostic",
              text,
              candidate_text: text,
              clickable_target_text: targetText,
              broad_context_text: norm((el.closest("main, section, article, body") || el).innerText || ""),
              tag: el.tagName || "",
              role: el.getAttribute("role") || "",
              href: el.getAttribute("href") || "",
              onclick: !!el.onclick || el.hasAttribute("onclick"),
              tabindex: el.getAttribute("tabindex") || "",
              rect: rectInfo(el),
              target_tag: target.tagName || "",
              target_role: target.getAttribute("role") || "",
              target_href: target.getAttribute("href") || "",
              target_onclick: !!target.onclick || target.hasAttribute("onclick"),
              target_tabindex: target.getAttribute("tabindex") || "",
              target_rect: rectInfo(target),
              rejected_reason: forbidden ? `forbidden_text:${{forbidden}}` : ""
            }};
            if (!isVisible(el) || !isVisible(target)) item.rejected_reason = item.rejected_reason || "not_visible";
            candidates.push({{item, target, score: tagScore(target), area: item.target_rect.width * item.target_rect.height}});
          }}
          candidates.sort((a, b) => a.score - b.score || a.area - b.area);
          if (!shouldClick) return candidates.slice(0, 120).map((entry) => entry.item);
          const selected = candidates.find((entry) => !entry.item.rejected_reason);
          if (!selected) return null;
          selected.target.click();
          return {{...selected.item, ok: true, selector: "js_text_action", text: selected.item.text, tag: selected.item.target_tag || selected.item.tag, role: selected.item.target_role || selected.item.role}};
        }}
        """

    def _download_retry_forbidden_texts(self):
        return [
            "Download",
            "Download Webex",
            "Webex 앱 다운로드",
            "Join on mobile",
            "모바일에서 참여",
            "모바일에서 참가",
            "Open Webex",
            "Webex 열기",
        ]

    async def _download_retry_html_snippet(self, radius=500):
        html_text = await self._safe_page_content(default="")
        if not html_text:
            return ""
        needles = [
            "Got it",
            "Try again",
            "Retry",
            "Join on mobile",
            "Download",
            "Webex Installer.dmg",
            "다시 시도",
            "모바일에서 참여",
            "Webex 앱 다운로드",
        ]
        lowered = html_text.lower()
        positions = [lowered.find(needle.lower()) for needle in needles if lowered.find(needle.lower()) >= 0]
        if not positions:
            return html_text[: min(len(html_text), radius * 2)]
        start = max(0, min(positions) - radius)
        end = min(len(html_text), max(positions) + radius)
        return html_text[start:end]

    async def _raise_download_retry_page_timeout(self, state):
        text = await self._visible_text_excerpt()
        extra = {
            "download_retry": state,
            "download_retry_keyword_hits": self._download_retry_keyword_hits(text),
            "visible_text": text,
            "url": self._safe_page_url(),
            "title": await self._safe_page_title(),
            "links_buttons_debug": await self._links_buttons_debug_info(),
            "input_debug": await self._input_debug_info(),
        }
        self._progress("webex_download_retry_page_timeout", extra)
        diagnostics = await self._maybe_await(
            self.collect_diagnostics(stage="webex_download_retry_page_timeout", extra=extra)
        )
        raise RuntimeError(
            "Webex download/retry page remained after "
            f"{state.get('max_attempts')} attempts. Visible text: {text}. Diagnostics: {diagnostics}"
        )

    def _download_retry_settle_sec(self):
        if "download_retry_settle_sec" in self.adapter_config():
            return max(0.0, float(self.adapter_config().get("download_retry_settle_sec", 2.0)))
        return max(0, self.timeout_ms("download_retry_settle_ms", 2000)) / 1000

    async def _click_try_again_near_problem_text(self):
        if not self._is_page_available() or not hasattr(self.page, "evaluate"):
            return None
        script = r"""
        () => {
          const problemPatterns = [
            /Problem joining from (your )?browser\?/i,
            /Having trouble joining from browser\?/i,
            /브라우저에서 (참여|참가)하는 데 문제가 있/
          ];
          const actionPatterns = [
            /^(Try again|Retry|Try again from browser)$/i,
            /^(Join from (your )?browser|Continue in browser)$/i,
            /^(브라우저에서 다시 시도|다시 시도|브라우저에서 참여|이 브라우저에서 참여)$/
          ];
          const unsafePatterns = [
            /Download/i,
            /Open Webex/i,
            /Join on mobile/i,
            /Webex 앱 다운로드/,
            /Webex 열기/,
            /모바일에서 (참여|참가)/
          ];
          const textOf = (el) => (el && (el.innerText || el.textContent || el.getAttribute("aria-label") || "") || "").replace(/\s+/g, " ").trim();
          const isVisible = (el) => {
            if (!el || !el.isConnected) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
          };
          const clickables = (root) => Array.from(root.querySelectorAll("button, a, [role='button']"));
          const problem = Array.from(document.querySelectorAll("body *")).find((el) => {
            const text = textOf(el);
            return text && problemPatterns.some((pattern) => pattern.test(text));
          });
          const roots = [];
          if (problem) {
            let node = problem;
            for (let depth = 0; node && depth < 6; depth += 1, node = node.parentElement) {
              roots.push(node);
            }
          }
          roots.push(document.body);
          for (const root of roots) {
            const candidates = clickables(root).filter((el) => {
              const text = textOf(el);
              return isVisible(el)
                && text
                && actionPatterns.some((pattern) => pattern.test(text))
                && !unsafePatterns.some((pattern) => pattern.test(text));
            });
            if (candidates.length) {
              const el = candidates[0];
              const text = textOf(el);
              el.click();
              return {
                selector: "dom-near-problem-text",
                text,
                tag: el.tagName,
                href: el.getAttribute("href") || "",
                role: el.getAttribute("role") || "",
                aria_label: el.getAttribute("aria-label") || ""
              };
            }
          }
          return null;
        }
        """
        try:
            return await self._maybe_await(self.page.evaluate(script))
        except self._safe_playwright_errors() as exc:
            self._last_page_metadata_error = exc
            self._append_browser_log("webex_try_again_dom_click_failed", repr(exc))
            return None
        except Exception as exc:
            self._append_browser_log("webex_try_again_dom_click_failed", repr(exc))
            return None

    def _unsafe_download_retry_selector(self, selector):
        lowered = str(selector or "").lower()
        unsafe_tokens = (
            "download",
            "open webex",
            "webex 열기",
            "webex 앱 다운로드",
            "join on mobile",
            "모바일에서 참여",
            "모바일에서 참가",
        )
        return any(token in lowered for token in unsafe_tokens)

    def _download_retry_keyword_hits(self, text):
        text = str(text or "")
        keywords = (
            "Get ready to join",
            'Open "Webex Installer.dmg" after it downloads',
            "Open Webex Installer.dmg after it downloads",
            "Problem joining from browser?",
            "Problem joining from your browser?",
            "Try again",
            "Retry",
            "Join on mobile",
            "Webex Installer.dmg",
            "Download Webex",
            "Download the Webex app",
            "Download",
            "Webex 앱 다운로드",
            "브라우저에서 참여하는 데 문제가 있",
            "브라우저에서 참가하는 데 문제가 있",
            "확인",
            "알겠습니다",
            "다시 시도",
            "모바일에서 참여",
            "모바일에서 참가",
        )
        return [keyword for keyword in keywords if keyword in text]

    def _download_retry_text_groups(self):
        installer_keywords = {
            "Get ready to join",
            'Open "Webex Installer.dmg" after it downloads',
            "Open Webex Installer.dmg after it downloads",
            "Webex Installer.dmg",
            "Download Webex",
            "Download the Webex app",
            "Webex 앱 다운로드",
        }
        return {
            "download_page_indicator": installer_keywords,
            "installer_download_indicator": installer_keywords,
            "problem_joining_from_browser": {
                "Problem joining from browser?",
                "Problem joining from your browser?",
                "브라우저에서 참여하는 데 문제가 있",
                "브라우저에서 참가하는 데 문제가 있",
            },
            "try_again_button": {"Try again", "Retry", "다시 시도"},
            "got_it_button": {"Got it", "확인", "알겠습니다"},
            "join_on_mobile_indicator": {"Join on mobile", "모바일에서 참여", "모바일에서 참가"},
            "app_download_indicator": {"Download", "Download Webex", "Webex Installer.dmg", "Webex 앱 다운로드"},
        }

    async def _links_buttons_debug_info(self):
        if not self._is_page_available() or not hasattr(self.page, "evaluate"):
            return []
        script = r"""
        () => Array.from(document.querySelectorAll("button, a, [role='button']")).slice(0, 80).map((el) => {
          const style = window.getComputedStyle(el);
          const rect = el.getBoundingClientRect();
          return {
            text: (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim(),
            tag: el.tagName,
            href: el.getAttribute("href") || "",
            role: el.getAttribute("role") || "",
            aria_label: el.getAttribute("aria-label") || "",
            visible: style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0,
            enabled: !el.disabled && el.getAttribute("aria-disabled") !== "true"
          };
        })
        """
        try:
            result = await self._maybe_await(self.page.evaluate(script))
            return result if isinstance(result, list) else []
        except self._safe_playwright_errors() as exc:
            self._last_page_metadata_error = exc
            self._append_browser_log("links_buttons_debug_unavailable", repr(exc))
            return []
        except Exception as exc:
            self._append_browser_log("links_buttons_debug_unavailable", repr(exc))
            return []

    async def _scan_joined_state_all_surfaces(self, timeout_ms=250):
        self._progress("webex_joined_state_scan_result", {"status": "scan_start"})
        try:
            scan_timeout = self._page_state_detection_timeout_sec()
            if timeout_ms is not None:
                scan_timeout = min(scan_timeout, max(0.05, float(timeout_ms) / 1000 * 8))
            snapshot = await asyncio.wait_for(
                self._page_state_snapshot(timeout_ms=timeout_ms),
                timeout=scan_timeout,
            )
        except asyncio.TimeoutError:
            snapshot = {}
            self._progress("webex_joined_state_scan_result", {"status": "scan_timeout"})
        except Exception as exc:
            snapshot = {}
            self._append_browser_log("webex_joined_state_scan_failed", repr(exc))

        scopes = [snapshot.get("page") or {}] + list(snapshot.get("frames") or [])
        for details in scopes:
            state = self._joined_state_from_snapshot_scope(details)
            if state:
                state = self._normalize_join_state(state)
                if state["status"] == "waiting_for_others":
                    self._progress("webex_waiting_for_others_detected", state)
                elif state["status"] == "waiting_for_host":
                    self._progress("webex_waiting_for_host_detected", state)
                elif state["status"] == "lobby":
                    self._progress("webex_lobby_detected", state)
                else:
                    self._progress("webex_joined_state_scan_result", state)
                return state

        window_state = self._detect_webex_meeting_window_state()
        if window_state:
            window_state = self._normalize_join_state(window_state)
            self._progress("webex_joined_state_detected_by_window_fallback", window_state)
            if self._is_terminal_join_state(window_state):
                self._progress("webex_joined_state_scan_result", window_state)
            return window_state

        self._progress("webex_joined_state_scan_result", {"status": "continue"})
        return {"status": "continue", "selector": None, "visible_text": ""}

    def _joined_state_from_snapshot_scope(self, details):
        if not isinstance(details, Mapping):
            return None
        visible_text = str(details.get("visible_text") or "")
        hidden_text = str(details.get("hidden_text") or "")
        source = {
            "source": "snapshot",
            "scope": details.get("scope") or "page",
            "url": str(details.get("url") or ""),
            "title": str(details.get("title") or ""),
            "visible_text": visible_text[:2000],
        }
        waiting_phrase = self._waiting_for_others_phrase(visible_text) or self._waiting_for_others_phrase(hidden_text)
        if waiting_phrase:
            return {
                "status": "waiting_for_others",
                "joined": True,
                "joined_source": "waiting_for_others_text",
                "selector": f"text:{waiting_phrase}",
                "joined_selector": self._media_control_selector_from_snapshot(details),
                "matched_text": waiting_phrase,
                **source,
            }
        waiting_host_phrase = self._waiting_for_host_phrase(visible_text) or self._waiting_for_host_phrase(hidden_text)
        if waiting_host_phrase:
            return {
                "status": "waiting_for_host",
                "joined": True,
                "joined_source": "waiting_for_host_text",
                "selector": f"text:{waiting_host_phrase}",
                "joined_selector": self._media_control_selector_from_snapshot(details),
                "matched_text": waiting_host_phrase,
                **source,
            }
        lobby_phrase = self._lobby_phrase(visible_text) or self._lobby_phrase(hidden_text)
        if lobby_phrase:
            return {
                "status": "lobby",
                "joined": False,
                "joined_source": "lobby_text",
                "selector": f"text:{lobby_phrase}",
                "joined_selector": self._media_control_selector_from_snapshot(details),
                "matched_text": lobby_phrase,
                **source,
            }
        error_phrase = self._join_error_phrase(visible_text) or self._join_error_phrase(hidden_text)
        if error_phrase:
            return {
                "status": "join_error",
                "joined": False,
                "joined_source": "join_error_text",
                "selector": f"text:{error_phrase}",
                "matched_text": error_phrase,
                **source,
            }
        media_selector = self._media_control_selector_from_snapshot(details)
        if media_selector:
            return {
                "status": "joined",
                "joined": True,
                "joined_source": "media_controls",
                "selector": media_selector,
                "joined_selector": media_selector,
                "action": "joined_media_controls",
                **source,
            }
        lowered_title = source["title"].lower()
        if "미팅 중" in lowered_title or re.search(r"(?<![a-z])in meeting\b", lowered_title) or "meeting in progress" in lowered_title:
            return {
                "status": "joined",
                "joined": True,
                "joined_source": "title",
                "selector": "title:joined",
                "joined_selector": "title:joined",
                **source,
            }
        return None

    def _waiting_for_others_phrase(self, text):
        normalized = " ".join(str(text or "").split())
        lowered = normalized.lower()
        phrases = (
            "Waiting for others to join",
            "Waiting for others",
            "You're the only one here",
            "You are the only one here",
            "No one else is here",
            "다른 사용자가 참여할 때까지 기다리는 중",
            "다른 사람이 참여할 때까지 기다리는 중",
            "참여할 때까지 기다리는 중",
            "기다리는 중",
        )
        for phrase in phrases:
            if phrase.lower() in lowered:
                return phrase
        return None

    def _waiting_for_host_phrase(self, text):
        normalized = " ".join(str(text or "").split())
        lowered = normalized.lower()
        phrases = (
            "Waiting for the host",
            "Waiting for host",
            "Waiting for the organizer",
            "Waiting for organizer",
            "The host has not joined",
            "호스트가 참여할 때까지 기다리는 중",
            "호스트를 기다리는 중",
        )
        for phrase in phrases:
            if phrase.lower() in lowered:
                return phrase
        return None

    def _lobby_phrase(self, text):
        normalized = " ".join(str(text or "").split())
        lowered = normalized.lower()
        phrases = (
            "You're in the lobby",
            "You are in the lobby",
            "Please wait, the host will let you in",
            "waiting room",
            "로비",
            "대기실",
            "호스트가 곧 입장시켜",
        )
        for phrase in phrases:
            if phrase.lower() in lowered:
                return phrase
        return None

    def _join_error_phrase(self, text):
        normalized = " ".join(str(text or "").split())
        lowered = normalized.lower()
        phrases = (
            "unable to join",
            "cannot join",
            "meeting is locked",
            "removed from the meeting",
            "meeting has ended",
            "invalid meeting",
            "미팅에 참여할 수",
            "회의에 참여할 수",
            "미팅이 잠겼",
            "회의가 잠겼",
        )
        for phrase in phrases:
            if phrase.lower() in lowered:
                return phrase
        return None

    def _media_control_selector_from_snapshot(self, details):
        controls = details.get("visible_buttons_links") or []
        leave_needles = (
            "leave meeting",
            "leave",
            "end meeting",
            "나가기",
            "미팅 나가기",
            "회의 나가기",
        )
        supporting_needles = (
            "mute",
            "unmute",
            "microphone",
            "start video",
            "stop video",
            "camera",
            "share content",
            "share screen",
            "음소거",
            "마이크",
            "비디오",
            "카메라",
            "공유",
        )
        fallback = None
        for item in controls:
            if not isinstance(item, Mapping):
                continue
            if self._snapshot_control_text_matches(item, leave_needles):
                element_id = str(item.get("id") or "")
                return f"#{element_id}" if element_id else "media_control"
            if fallback is None and self._snapshot_control_text_matches(item, supporting_needles):
                element_id = str(item.get("id") or "")
                fallback = f"#{element_id}" if element_id else "media_control"
        if fallback and any(
            self._snapshot_control_text_matches(item, leave_needles)
            for item in controls
            if isinstance(item, Mapping)
        ):
            return fallback
        return None

    def _detect_webex_meeting_window_state(self):
        if platform.system() != "Linux":
            return None
        if not shutil.which("wmctrl"):
            return None
        display = self._configured_display()
        env = dict(os.environ)
        if display:
            env["DISPLAY"] = display
        try:
            result = subprocess.run(["wmctrl", "-lG"], capture_output=True, text=True, timeout=2, check=False, env=env)
        except Exception as exc:
            self._append_browser_log("webex_window_fallback_unavailable", repr(exc))
            return None
        if result.returncode != 0:
            return None
        for line in (result.stdout or "").splitlines():
            lower = line.lower()
            if "webex" not in lower and "chrome" not in lower and "chromium" not in lower:
                continue
            if not any(token in lower for token in ("webex", "meeting", "미팅", "회의")):
                continue
            is_get_ready = "get ready to join" in lower
            title_joined = self._post_final_join_title_is_joined(line)
            joined = (
                title_joined
                or (
                    self._browser_join_clicked_once
                    and not is_get_ready
                    and ("cisco webex" in lower or "webex meeting" in lower or "webex - chromium" in lower)
                )
            )
            status = "joined" if joined else "candidate"
            details = {
                "status": status,
                "joined": bool(joined),
                "joined_source": "window_fallback" if joined else None,
                "selector": "window_fallback",
                "joined_selector": "window_fallback" if joined else None,
                "window": line,
            }
            self._progress("webex_meeting_window_detected", details)
            return details
        return None

    async def _prejoin_state(self, timeout_ms=250):
        joined_state = await self._scan_joined_state_all_surfaces(timeout_ms=timeout_ms)
        if self._is_terminal_join_state(joined_state):
            return joined_state
        for status, group in (
            ("final_join", "final_join_fast"),
            ("final_join", "start_meeting_button"),
            ("final_join", "join_button"),
            ("joined", "joined_indicator"),
            ("waiting_for_others", "waiting_for_others_indicator"),
            ("lobby", "lobby_indicator"),
            ("blocked", "blocked_indicator"),
        ):
            selector = await self._first_visible_selector(group, timeout_ms=timeout_ms)
            if selector:
                return {
                    "status": status,
                    "selector": selector,
                    "visible_text": await self._visible_text_excerpt(),
                }
        return {"status": "continue", "selector": None, "visible_text": ""}

    async def _apply_in_meeting_initial_media_state(self):
        if bool(self.adapter_config().get("skip_device_selection", False)):
            self._warn_control_event(
                "webex_post_join_media_check_skipped",
                {"reason": "skip_device_selection"},
            )
            return False

        if bool(self.adapter_config().get("initial_microphone_enabled", True)):
            await self.unmute_microphone()
        else:
            await self.mute_microphone()

        if bool(self.adapter_config().get("initial_camera_enabled", True)):
            await self.start_camera()
        else:
            await self.stop_camera()
        return True

    async def _maybe_run_post_join_media_check(self, vtc_url):
        if not bool(self.adapter_config().get("post_join_media_check", False)):
            return None

        timeout_sec = float(self.adapter_config().get("post_join_media_check_timeout_sec", 2))
        try:
            media_ready = await asyncio.wait_for(
                self._apply_in_meeting_initial_media_state(),
                timeout=max(0.1, timeout_sec),
            )
            if media_ready:
                self._notify_media_ready(vtc_url)
            return bool(media_ready)
        except (asyncio.TimeoutError, RuntimeError) as exc:
            details = {"error": repr(exc), "timeout_sec": timeout_sec}
            self._warn_control_event("webex_post_join_media_state_unverified", details)
            if bool(self.adapter_config().get("strict_post_join_media_state", False)):
                await self._maybe_await(
                    self.collect_diagnostics(stage="webex_post_join_media_state_unverified", extra=details)
                )
            if bool(self.adapter_config().get("fail_on_post_join_media_unverified", False)):
                raise
            return False

    async def _apply_prejoin_media_preferences(self):
        if not bool(self.adapter_config().get("apply_initial_media_state_in_prejoin", True)):
            return False

        changed = False
        changed = bool(
            await self._set_prejoin_media_state(
                name="microphone",
                desired=bool(self.adapter_config().get("initial_microphone_enabled", True)),
                on_group="prejoin_mic_on_indicator",
                off_group="prejoin_mic_off_indicator",
            )
        ) or changed
        changed = bool(
            await self._set_prejoin_media_state(
                name="camera",
                desired=bool(self.adapter_config().get("initial_camera_enabled", True)),
                on_group="prejoin_camera_on_indicator",
                off_group="prejoin_camera_off_indicator",
            )
        ) or changed
        return changed

    async def _set_prejoin_media_state(self, name, desired, on_group, off_group):
        timeout_ms = self.timeout_ms("prejoin_media_state_timeout_ms", self.timeout_ms("optional_selector_timeout_ms", 1000))
        current = await self._detect_binary_state(on_group, off_group, timeout_ms=timeout_ms)
        if current is desired:
            return False

        action_group = off_group if desired else on_group
        clicked = await self._click_first_visible(action_group, timeout_ms=timeout_ms)
        verified = current
        if clicked:
            await asyncio.sleep(float(self.adapter_config().get("prejoin_media_state_settle_sec", 0.2)))
            verified = await self._detect_binary_state(on_group, off_group, timeout_ms=timeout_ms)
            if verified is desired:
                emit_event(
                    self.config,
                    "webex_prejoin_media_state_changed",
                    {"control": name, "desired": desired, "selector": clicked},
                    self.service_name,
                )
                return True

        return await self._handle_unverified_prejoin_media_state(
            name=name,
            desired=desired,
            current=current,
            verified=verified,
            clicked=clicked,
            on_group=on_group,
            off_group=off_group,
        )

    async def _handle_unverified_prejoin_media_state(self, **details):
        if not details.get("clicked") and details.get("current") is None:
            return False

        strict = bool(self.adapter_config().get("strict_prejoin_media_state", False))
        fail = bool(self.adapter_config().get("fail_on_prejoin_media_state_unverified", False))
        self._warn_control_event("webex_prejoin_media_state_unverified", details)
        if strict or fail:
            await self._maybe_await(
                self.collect_diagnostics(stage="webex_prejoin_media_state_unverified", extra=details)
            )
        if fail:
            raise RuntimeError(
                f"Webex prejoin {details.get('name')} state could not be verified."
            )
        return False

    async def leave(self):
        self._progress("webex_leave_attempt")
        clicked = await self._click_optional("leave_button", "webex_leave_clicked")
        fallback_success = False
        if clicked:
            await self._click_optional("leave_confirm", "webex_leave_confirmed")
        else:
            fallback_success = await self._fallback_action("leave")
            if not fallback_success:
                fallback_success = await self._fallback_close_meeting_surface()
        success = bool(clicked or fallback_success)
        self._progress("webex_leave_done", {"success": success, "clicked": bool(clicked), "fallback_success": bool(fallback_success)})
        emit_event(self.config, "webex_left_meeting", {"success": success}, self.service_name)
        return success

    async def _fallback_close_meeting_surface(self):
        details = {"success": False}
        try:
            if self._is_page_available() and hasattr(self.page, "close"):
                await self._maybe_await(self.page.close())
                details = {"success": True, "target": "page"}
                self.page = None
            elif self.context is not None and hasattr(self.context, "close"):
                await self._maybe_await(self.context.close())
                details = {"success": True, "target": "context"}
                self.context = None
                self.page = None
            elif self.browser is not None and hasattr(self.browser, "close"):
                await self._maybe_await(self.browser.close())
                details = {"success": True, "target": "browser"}
                self.browser = None
                self.page = None
        except Exception as exc:
            details = {"success": False, "error": repr(exc)}
            self._append_browser_log("webex_leave_fallback_close_failed", repr(exc))
        self._progress("webex_leave_fallback_close", details)
        return bool(details.get("success"))

    async def close(self):
        self._flush_browser_log()
        if self.adapter_config().get("diagnostic_dir") or self.adapter_config().get("diagnostics_dir"):
            try:
                await self.collect_diagnostics(stage="close")
            except Exception:
                pass
        if self.context is not None:
            await self.context.close()
            self.context = None
        if self.browser is not None:
            await self.browser.close()
            self.browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        self.page = None
        emit_event(self.config, "adapter_closed", {"backend": "playwright"}, self.service_name)
        return True

    def run_sanity_checks(self):
        strict = bool(self.adapter_config().get("strict_sanity_checks", False))
        warnings = []
        checks = []

        emit_event(self.config, "webex_sanity_check_started", {}, self.service_name)

        def record(name, ok, message="", optional=False):
            item = {"name": name, "ok": bool(ok), "message": message, "optional": bool(optional)}
            checks.append(item)
            if not ok:
                warnings.append(item)
                emit_event(self.config, "webex_sanity_check_warning", item, self.service_name)

        display = self._configured_display()
        record("display", bool(display), "DISPLAY is not configured")
        if shutil.which("xdpyinfo"):
            result = self._run_sanity_command(["xdpyinfo"], env_display=display)
            record("xdpyinfo", result.returncode == 0, result.stderr.strip() or result.stdout.strip(), optional=True)
        else:
            record("xdpyinfo", False, "xdpyinfo not found", optional=True)

        if shutil.which("pactl"):
            pactl_info = self._run_sanity_command(["pactl", "info"])
            record("pactl_info", pactl_info.returncode == 0, pactl_info.stderr.strip(), optional=True)
            sources = self._run_sanity_command(["pactl", "list", "short", "sources"])
            configured_source = self.adapter_config().get("microphone_source") or self.adapter_config().get("audio_source")
            if configured_source:
                record("microphone_source", str(configured_source) in sources.stdout, f"{configured_source} not found")
        else:
            record("pactl_info", False, "pactl not found", optional=True)

        video_expected = bool(self.adapter_config().get("initial_camera_enabled", True) or self.adapter_config().get("video_device") or self.config.get("video_device"))
        if video_expected:
            record("video_device", bool(glob.glob("/dev/video*")), "no /dev/video* devices found", optional=True)

        record("ffmpeg", bool(shutil.which("ffmpeg")), "ffmpeg not found", optional=True)
        executable = self.launch_options().get("executable_path")
        channel = self.launch_options().get("channel") or self.adapter_config().get("browser_channel")
        record("browser_executable_or_channel", bool(executable or channel), "no Chrome/Chromium executable or channel configured/found", optional=True)

        fallback = self.adapter_config().get("fallback", {})
        needs_window_tools = bool(
            self.adapter_config().get("screen_share_target")
            or self.adapter_config().get("auto_select_desktop_capture_source")
            or (isinstance(fallback, Mapping) and fallback.get("enabled"))
        )
        if needs_window_tools:
            record("xdotool", bool(shutil.which("xdotool")), "xdotool not found", optional=True)
            record("wmctrl", bool(shutil.which("wmctrl")), "wmctrl not found", optional=True)

        self.sanity_check_results = checks
        emit_event(
            self.config,
            "webex_sanity_check_completed",
            {"warnings": len(warnings), "checks": checks},
            self.service_name,
        )
        if strict and warnings:
            raise RuntimeError(f"Webex sanity checks failed: {warnings}")
        return {"warnings": warnings, "checks": checks}

    async def collect_diagnostics(self, stage=None, extra=None):
        diag_dir = self._diagnostic_dir()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_stage = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(stage or "diagnostics"))
        prefix = f"{stamp}_{safe_stage}"
        diag_dir.mkdir(parents=True, exist_ok=True)
        saved = {"directory": str(diag_dir), "files": {}, "errors": {}}

        page = self.page
        metadata = {
            "stage": stage,
            "extra": dict(extra or {}),
            "url": None,
            "title": None,
            "states": {
                "mic": self.mic_enabled,
                "camera": self.camera_enabled,
                "screen_share": self.screen_sharing,
                "joined": self._meeting_joined_notified,
            },
            "adapter_config": self._safe_adapter_config(),
            "browser_log": list(self.browser_log),
            "sanity_check_results": list(self.sanity_check_results),
            "external_protocol_dismiss_attempts": list(self.external_protocol_dismiss_attempts),
            "external_protocol_suppression_profile_path": self.external_protocol_profile_path,
            "commands": {},
        }

        if page is not None:
            metadata["url"] = self._safe_page_url()
            metadata["title"] = await self._safe_page_title()
            metadata["visible_text"] = await self._safe_visible_text_excerpt()
            metadata["input_debug"] = await self._input_debug_info()
            screenshot_path = diag_dir / f"{prefix}.png"
            if await self._safe_screenshot(path=str(screenshot_path)):
                saved["files"]["screenshot"] = str(screenshot_path)
            html = await self._safe_page_content()
            if html:
                html_path = diag_dir / f"{prefix}.html"
                try:
                    html_path.write_text(html, encoding="utf-8")
                    saved["files"]["html"] = str(html_path)
                except Exception as exc:
                    saved["errors"]["html"] = repr(exc)

        for name, command in self._diagnostic_commands().items():
            command_path = diag_dir / f"{prefix}.{name}.txt"
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
                metadata["commands"][name] = {
                    "command": command,
                    "returncode": result.returncode,
                    "stdout_path": str(command_path),
                    "stderr": result.stderr,
                }
                command_path.write_text(result.stdout or result.stderr or "", encoding="utf-8", errors="replace")
                saved["files"][name] = str(command_path)
            except Exception as exc:
                metadata["commands"][name] = {"command": command, "error": repr(exc)}
                saved["errors"][name] = repr(exc)

        metadata_path = diag_dir / f"{prefix}.metadata.json"
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True, default=str), encoding="utf-8")
        saved["files"]["metadata"] = str(metadata_path)
        emit_event(self.config, "webex_diagnostics_saved", {"stage": stage, "path": str(metadata_path)}, self.service_name)
        return saved

    async def is_in_meeting(self):
        if bool(getattr(self, "in_meeting", False)) or bool(getattr(self, "joined", False)):
            return True
        if self.page is None:
            return False
        if await self._title_indicates_joined():
            return True
        try:
            await self.wait_for_any_visible(self.page, self.selectors("joined"), timeout=1000)
            return True
        except RuntimeError:
            return False

    async def mute_microphone(self):
        return await self._set_binary_control_state(
            name="microphone",
            desired=False,
            on_group="mic_on_indicator",
            off_group="mic_off_indicator",
            cached_attr="mic_enabled",
        )

    async def unmute_microphone(self):
        return await self._set_binary_control_state(
            name="microphone",
            desired=True,
            on_group="mic_on_indicator",
            off_group="mic_off_indicator",
            cached_attr="mic_enabled",
        )

    async def start_camera(self):
        return await self._set_binary_control_state(
            name="camera",
            desired=True,
            on_group="camera_on_indicator",
            off_group="camera_off_indicator",
            cached_attr="camera_enabled",
        )

    async def stop_camera(self):
        return await self._set_binary_control_state(
            name="camera",
            desired=False,
            on_group="camera_on_indicator",
            off_group="camera_off_indicator",
            cached_attr="camera_enabled",
        )

    async def start_screen_share(self):
        timeout_ms = self.timeout_ms("control_state_timeout_ms", 500)
        if await self._visible_optional("screen_share_on_indicator", timeout_ms=timeout_ms):
            self.screen_sharing = True
            emit_event(self.config, "webex_screen_share_started", {"success": True, "already_active": True}, self.service_name)
            return True

        clicked = await self._click_first_visible("screen_share_button", timeout_ms=timeout_ms)
        fallback_success = False
        if clicked:
            emit_event(self.config, "webex_screen_share_start_clicked", {"selector": clicked, "success": True}, self.service_name)
            await self._click_optional("share_target", "webex_screen_share_target_clicked")
            await self._click_optional("share_confirm", "webex_screen_share_confirmed")
        elif self._control_fallback_enabled():
            fallback_success = await self._fallback_action("screen_share")

        if clicked or fallback_success:
            await asyncio.sleep(float(self.adapter_config().get("control_state_settle_sec", 0.5)))
            if await self._visible_optional("screen_share_on_indicator", timeout_ms=timeout_ms):
                self.screen_sharing = True
                emit_event(self.config, "webex_screen_share_started", {"success": True, "selector": clicked, "fallback": fallback_success}, self.service_name)
                return True

        return await self._handle_unverified_control_state(
            name="screen_share_start",
            desired=True,
            cached_attr="screen_sharing",
            stage="webex_screen_share_start_unverified",
            extra={"clicked": clicked, "fallback_success": fallback_success},
        )

    async def stop_screen_share(self):
        timeout_ms = self.timeout_ms("control_state_timeout_ms", 500)
        if not await self._visible_optional("screen_share_on_indicator", timeout_ms=timeout_ms):
            self.screen_sharing = False
            emit_event(self.config, "webex_screen_share_stopped", {"success": True, "already_inactive": True}, self.service_name)
            return True

        clicked = await self._click_first_visible("screen_share_stop", timeout_ms=timeout_ms)
        fallback_success = False
        if clicked:
            emit_event(self.config, "webex_screen_share_stop_clicked", {"selector": clicked, "success": True}, self.service_name)
        elif self._control_fallback_enabled():
            fallback_success = await self._fallback_action("screen_share")

        if clicked or fallback_success:
            await asyncio.sleep(float(self.adapter_config().get("control_state_settle_sec", 0.5)))
            if not await self._visible_optional("screen_share_on_indicator", timeout_ms=timeout_ms):
                self.screen_sharing = False
                emit_event(self.config, "webex_screen_share_stopped", {"success": True, "selector": clicked, "fallback": fallback_success}, self.service_name)
                return True

        return await self._handle_unverified_control_state(
            name="screen_share_stop",
            desired=False,
            cached_attr="screen_sharing",
            stage="webex_screen_share_stop_unverified",
            extra={"clicked": clicked, "fallback_success": fallback_success},
        )

    async def _set_button_state(self, state_attr, desired, selector_group, fallback_name, event_name):
        on_group = {
            "mic_enabled": "mic_on_indicator",
            "camera_enabled": "camera_on_indicator",
        }.get(state_attr, selector_group)
        off_group = {
            "mic_enabled": "mic_off_indicator",
            "camera_enabled": "camera_off_indicator",
        }.get(state_attr, selector_group)
        name = "microphone" if state_attr == "mic_enabled" else "camera" if state_attr == "camera_enabled" else state_attr
        return await self._set_binary_control_state(name, desired, on_group, off_group, state_attr)

    async def _detect_binary_state(self, on_group, off_group, timeout_ms=500):
        on_candidates = await self._visible_candidates(on_group, timeout_ms=timeout_ms)
        off_candidates = await self._visible_candidates(off_group, timeout_ms=timeout_ms)
        if on_candidates and not off_candidates:
            return True
        if off_candidates and not on_candidates:
            return False
        if not on_candidates and not off_candidates:
            return None

        combined = [(True, item) for item in on_candidates] + [(False, item) for item in off_candidates]
        enabled = [item for item in combined if item[1].get("enabled", True)] or combined
        enabled.sort(key=lambda item: self._selector_specificity_score(item[1]["selector"]), reverse=True)
        chosen_state, chosen = enabled[0]
        self._warn_control_event(
            "webex_control_state_ambiguous",
            {
                "on_group": on_group,
                "off_group": off_group,
                "chosen_state": chosen_state,
                "chosen_selector": chosen.get("selector"),
                "on_selectors": [item["selector"] for item in on_candidates],
                "off_selectors": [item["selector"] for item in off_candidates],
            },
        )
        return chosen_state

    async def _set_binary_control_state(self, name, desired, on_group, off_group, cached_attr, timeout_ms=500):
        timeout_ms = self.timeout_ms("control_state_timeout_ms", timeout_ms)
        current = await self._detect_binary_state(on_group, off_group, timeout_ms=timeout_ms)
        event_prefix = self._control_event_prefix(name)
        emit_event(
            self.config,
            f"webex_{event_prefix}_state_detected",
            {"state": current, "desired": desired, "cached_attr": cached_attr},
            self.service_name,
        )
        if current is desired:
            setattr(self, cached_attr, desired)
            return True

        action_group = off_group if desired else on_group
        clicked = await self._click_first_visible(action_group, timeout_ms=timeout_ms)
        fallback_success = False
        if clicked:
            emit_event(
                self.config,
                f"webex_{event_prefix}_state_action_clicked",
                {"selector": clicked, "desired": desired, "current": current},
                self.service_name,
            )
        elif self._control_fallback_enabled():
            fallback_success = await self._fallback_action(event_prefix)

        if clicked or fallback_success:
            await asyncio.sleep(float(self.adapter_config().get("control_state_settle_sec", 0.5)))
            verified = await self._detect_binary_state(on_group, off_group, timeout_ms=timeout_ms)
            if verified is desired:
                setattr(self, cached_attr, desired)
                emit_event(
                    self.config,
                    f"webex_{event_prefix}_state_changed",
                    {"state": desired, "selector": clicked, "fallback": fallback_success},
                    self.service_name,
                )
                return True
        else:
            verified = current

        return await self._handle_unverified_control_state(
            name=name,
            desired=desired,
            cached_attr=cached_attr,
            stage=f"webex_{name}_state_unverified",
            extra={
                "current": current,
                "verified": verified,
                "clicked": clicked,
                "fallback_success": fallback_success,
                "on_group": on_group,
                "off_group": off_group,
            },
        )

    async def _click_required(self, selector_group, event_name, timeout):
        if not self._is_page_available():
            raise RuntimeError(f"Webex page is unavailable while waiting for required selector group: {selector_group}")
        selector = await self.click_first_visible(self.page, self.selectors(selector_group), timeout=timeout)
        emit_event(self.config, event_name, {"selector": selector, "success": True}, self.service_name)
        return selector

    async def _click_optional(self, selector_group, event_name, timeout=None):
        if not self._is_page_available():
            return None
        timeout = self.timeout_ms("optional_selector_timeout_ms", 1000) if timeout is None else timeout
        selector = await self.click_if_visible(self.page, self.selectors(selector_group), timeout=timeout)
        if selector:
            emit_event(self.config, event_name, {"selector": selector, "success": True}, self.service_name)
        return selector

    def _page_locator_scopes(self, prefer_meeting_frame=True):
        frame_scopes = []
        scopes = []
        pages = self._known_playwright_pages()
        if not pages:
            return []
        for page_index, page in enumerate(pages):
            if not self._is_scope_available(page) or not hasattr(page, "locator"):
                continue
            page_scope = self._page_scope_name(page_index)
            scopes.append((page_scope, page))
            try:
                frames = getattr(page, "frames", []) or []
            except self._safe_playwright_errors() as exc:
                self._last_page_metadata_error = exc
                self._append_browser_log("page_frames_unavailable", repr(exc))
                continue
            except Exception as exc:
                self._last_page_metadata_error = exc
                self._append_browser_log("page_frames_unavailable", repr(exc))
                continue
            for index, frame in enumerate(frames):
                if frame is not page and hasattr(frame, "locator"):
                    frame_name = f"frame[{index}]" if page_index == 0 else f"{page_scope}.frame[{index}]"
                    frame_scopes.append((frame_name, frame))
        preferred = self._preferred_webex_meeting_frame if prefer_meeting_frame else None
        if preferred is not None:
            for item in list(frame_scopes):
                if item[1] is preferred:
                    frame_scopes.remove(item)
                    scopes.append(item)
                    break
        scopes.extend(frame_scopes)
        return scopes

    async def _visible_candidates(self, selector_group, timeout_ms=None, include_locator=False):
        timeout = self.timeout_ms("optional_selector_timeout_ms", 1000) if timeout_ms is None else timeout_ms
        candidates = []
        for selector in self.selectors(selector_group):
            for scope_name, scope in self._page_locator_scopes():
                try:
                    locator = scope.locator(selector).first
                    await locator.wait_for(state="visible", timeout=timeout)
                    enabled = True
                    if hasattr(locator, "is_enabled"):
                        try:
                            enabled = bool(await self._maybe_await(locator.is_enabled(timeout=timeout)))
                        except TypeError:
                            enabled = bool(await self._maybe_await(locator.is_enabled()))
                    candidate = {"selector": selector, "enabled": enabled, "scope": scope_name}
                    if include_locator:
                        candidate["_locator"] = locator
                    candidates.append(candidate)
                except PlaywrightTimeoutError:
                    continue
                except self._safe_playwright_errors() as exc:
                    self._last_page_metadata_error = exc
                    self._append_browser_log("selector_candidate_unavailable", repr(exc))
                    continue
                except Exception:
                    continue
        return candidates

    def _selector_specificity_score(self, selector):
        text = str(selector)
        score = len(text)
        for token in ("microphone", "camera", "video", "sharing", "content", "screen"):
            if token in text.lower():
                score += 20
        for token in ("Unmute", "Stop video", "Start video", "Stop sharing", "Share content", "Share screen"):
            if token in text:
                score += 30
        if text in {"[aria-label*=\"Share\"]", 'button:has-text("Share")'}:
            score -= 50
        return score

    def _control_event_prefix(self, name):
        return "mic" if name == "microphone" else str(name)

    def _control_fallback_enabled(self):
        fallback = self.adapter_config().get("fallback", {})
        return isinstance(fallback, Mapping) and bool(
            fallback.get("enabled", False)
            or self.adapter_config().get("allow_unverified_fallback", False)
            or fallback.get("allow_unverified_fallback", False)
        )

    def _trust_unverified_control_state(self):
        return bool(self.adapter_config().get("trust_click_state_after_unverified_action", False))

    def _warn_control_event(self, event_name, details):
        payload = {**dict(details or {}), "warning": True}
        self._append_browser_log(event_name, json.dumps(payload, sort_keys=True, default=str))
        emit_event(self.config, event_name, payload, self.service_name)

    async def _handle_unverified_control_state(self, name, desired, cached_attr, stage, extra=None):
        details = {
            "control": name,
            "desired": desired,
            "cached_attr": cached_attr,
            **dict(extra or {}),
        }
        await self._maybe_await(self.collect_diagnostics(stage=stage, extra=details))
        self._warn_control_event("webex_control_state_unverified", details)
        if self._trust_unverified_control_state():
            setattr(self, cached_attr, desired)
            self._warn_control_event(
                "webex_control_state_trusted_after_unverified_action",
                {**details, "trusted_cached_state": desired},
            )
            return True
        raise RuntimeError(
            f"Webex {name} state could not be verified after action; cached state was not updated."
        )

    async def _fill_optional(self, selector_group, value, event_name):
        if value in (None, ""):
            return None
        if not self._is_page_available():
            return None
        timeout = self.timeout_ms("optional_selector_timeout_ms", 1000)
        for selector in self.selectors(selector_group):
            locator = self.page.locator(selector).first
            try:
                await locator.wait_for(state="visible", timeout=timeout)
                await locator.fill(str(value))
                emit_event(self.config, event_name, {"selector": selector, "success": True}, self.service_name)
                return selector
            except PlaywrightTimeoutError:
                continue
        return None

    async def _fill_optional_configured(self, selector_group, config_key, event_name):
        return await self._fill_optional(selector_group, self.adapter_config().get(config_key), event_name)

    async def _wait_required(self, selector_group, timeout):
        if not self._is_page_available():
            raise RuntimeError(f"Webex page is unavailable while waiting for required selector group: {selector_group}")
        selector = await self.wait_for_any_visible(self.page, self.selectors(selector_group), timeout=timeout)
        emit_event(self.config, "webex_join_verified", {"selector": selector, "success": True}, self.service_name)
        return selector

    async def _visible_optional(self, selector_group, timeout_ms=None):
        return await self._first_visible_selector(selector_group, timeout_ms=timeout_ms) is not None

    async def _first_visible_selector(self, selector_group, timeout_ms=None):
        found = await self._first_visible_locator(selector_group, timeout_ms=timeout_ms)
        return found["selector"] if found else None

    async def _first_visible_locator(self, selector_group, timeout_ms=None):
        timeout = self.timeout_ms("optional_selector_timeout_ms", 1000) if timeout_ms is None else timeout_ms
        for selector in self.selectors(selector_group):
            for scope_name, scope in self._page_locator_scopes():
                try:
                    locator = scope.locator(selector).first
                    await locator.wait_for(state="visible", timeout=timeout)
                    return {"selector": selector, "locator": locator, "scope": scope_name}
                except PlaywrightTimeoutError:
                    continue
                except self._safe_playwright_errors() as exc:
                    self._last_page_metadata_error = exc
                    self._append_browser_log("selector_visibility_unavailable", repr(exc))
                    return None
        return None

    async def _click_browser_prejoin_selector(self, selector_group, timeout_ms=None):
        selector = await self._click_first_visible(selector_group, timeout_ms=timeout_ms)
        if selector:
            if selector_group in {"join_from_browser", "join_from_this_browser", "continue_in_browser", "use_web_app", "open_in_browser"}:
                self._browser_join_clicked_once = True
                self._progress("browser_join_clicked", {"selector": selector, "selector_group": selector_group})
            await self._dismiss_external_protocol_prompt(stage=f"after_click_{selector_group}")
        return selector

    async def _click_browser_join_from_state(self, state, timeout_ms=None):
        action = state.get("browser_join_action") or {}
        timeout = self._download_retry_action_timeout_ms(timeout_ms)
        total_timeout = self._browser_join_click_total_timeout_sec()
        scopes = self._page_locator_scopes(prefer_meeting_frame=False)
        preferred_scope_name = action.get("scope")
        attempt = {
            "selector": action.get("selector"),
            "scope": preferred_scope_name,
            "text": action.get("text"),
            "method": "candidate",
            "timeout_sec": total_timeout,
        }
        self._progress("browser_join_click_attempt", attempt)
        if action.get("selector"):
            try:
                clicked = await asyncio.wait_for(
                    self._click_browser_join_candidate(scopes, action, timeout),
                    timeout=total_timeout,
                )
            except asyncio.TimeoutError:
                self._progress("browser_join_click_timeout", attempt)
                return None
            if clicked:
                self._browser_join_clicked_once = True
                self._progress("browser_join_click_success", clicked)
                return clicked
            self._progress("browser_join_click_failed", attempt)
            return None

        preferred = [item for item in scopes if item[0] == preferred_scope_name]
        ordered_scopes = preferred + [item for item in scopes if item[0] != preferred_scope_name]
        selector_groups = (
            "join_from_browser",
            "join_from_this_browser",
            "continue_in_browser",
            "use_web_app",
            "open_in_browser",
        )
        deadline = asyncio.get_running_loop().time() + total_timeout
        try:
            for scope_name, scope in ordered_scopes:
                for selector_group in selector_groups:
                    for selector in self.selectors(selector_group):
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            self._progress("browser_join_click_timeout", attempt)
                            return None
                        clicked = await asyncio.wait_for(
                            self._click_selector_in_scope(scope_name, scope, selector, timeout),
                            timeout=max(0.01, remaining),
                        )
                        if clicked:
                            clicked["selector_group"] = selector_group
                            self._browser_join_clicked_once = True
                            self._progress("browser_join_click_success", clicked)
                            return clicked
        except asyncio.TimeoutError:
            self._progress("browser_join_click_timeout", attempt)
            return None
        self._progress("browser_join_click_not_found", {"preferred_scope": preferred_scope_name})
        return None

    def _browser_join_click_total_timeout_sec(self):
        try:
            return max(
                0.01,
                float(
                    self.adapter_config().get(
                        "browser_join_click_total_timeout_sec",
                        self.adapter_config().get("download_retry_click_total_timeout_sec", 5.0),
                    )
                ),
            )
        except (TypeError, ValueError):
            return 5.0

    async def _click_browser_join_candidate(self, scopes, action, timeout_ms):
        selector = action.get("selector")
        scope_name = action.get("scope")
        scope = next((candidate_scope for candidate_name, candidate_scope in scopes if candidate_name == scope_name), None)
        if scope is None:
            self._progress(
                "browser_join_click_failed",
                {"selector": selector, "scope": scope_name, "text": action.get("text"), "method": "scope_lookup", "reason": "scope_not_found"},
            )
            return None

        clicked = await self._js_click_browser_join_candidate(scope_name, scope, selector, action.get("text"), timeout_ms)
        if clicked:
            return clicked

        methods = (
            ("playwright_locator_click", {"timeout": timeout_ms}),
            ("playwright_force_click", {"timeout": timeout_ms, "force": True}),
        )
        locator = scope.locator(selector).first
        for method, kwargs in methods:
            try:
                await locator.click(**kwargs)
                return {"ok": True, "selector": selector, "scope": scope_name, "text": action.get("text"), "method": method}
            except PlaywrightTimeoutError as exc:
                self._progress(
                    "browser_join_click_failed",
                    {"selector": selector, "scope": scope_name, "text": action.get("text"), "method": method, "reason": "timeout", "error": repr(exc)},
                )
            except self._safe_playwright_errors() as exc:
                self._last_page_metadata_error = exc
                self._append_browser_log("browser_join_candidate_click_unavailable", repr(exc))
                self._progress(
                    "browser_join_click_failed",
                    {"selector": selector, "scope": scope_name, "text": action.get("text"), "method": method, "reason": "playwright_error", "error": repr(exc)},
                )
            except Exception as exc:
                self._append_browser_log("browser_join_candidate_click_unavailable", repr(exc))
                self._progress(
                    "browser_join_click_failed",
                    {"selector": selector, "scope": scope_name, "text": action.get("text"), "method": method, "reason": "error", "error": repr(exc)},
                )

        try:
            box = await asyncio.wait_for(self._maybe_await(locator.bounding_box()), timeout=timeout_ms / 1000)
            if box:
                x = float(box.get("x", 0)) + float(box.get("width", 0)) / 2
                y = float(box.get("y", 0)) + float(box.get("height", 0)) / 2
                mouse = getattr(scope, "mouse", None) or getattr(self.page, "mouse", None)
                if mouse and hasattr(mouse, "click"):
                    await asyncio.wait_for(self._maybe_await(mouse.click(x, y)), timeout=timeout_ms / 1000)
                    return {"ok": True, "selector": selector, "scope": scope_name, "text": action.get("text"), "method": "coordinate_click", "x": x, "y": y}
        except Exception as exc:
            self._append_browser_log("browser_join_candidate_coordinate_click_unavailable", repr(exc))
        self._progress(
            "browser_join_click_failed",
            {"selector": selector, "scope": scope_name, "text": action.get("text"), "method": "coordinate_click", "reason": "no_clickable_box"},
        )
        return None

    async def _js_click_browser_join_candidate(self, scope_name, scope, selector, text, timeout_ms):
        script = r"""
        (selector) => {
          const visible = (el) => {
            if (!el || !el.isConnected) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
          };
          const norm = (value) => String(value || "").replace(/\s+/g, " ").trim();
          const target = document.querySelector(selector);
          if (!target) return {ok: false, method: "js_candidate_click", reason: "selector_not_found"};
          const clickables = Array.from(target.querySelectorAll("button, a, [role='button'], [role='link'], [tabindex], mdc-button"));
          const ancestors = [];
          let node = target;
          while (node && node !== document.body && ancestors.length < 8) {
            ancestors.push(node);
            node = node.parentElement;
          }
          const candidates = [target, ...clickables, ...ancestors].filter((el, index, all) => el && all.indexOf(el) === index);
          const clickable = candidates.find(visible);
          if (!clickable) return {ok: false, method: "js_candidate_click", reason: "not_visible"};
          clickable.scrollIntoView({block: "center", inline: "center"});
          clickable.click();
          const rect = clickable.getBoundingClientRect();
          return {
            ok: true,
            method: "js_candidate_click",
            selector,
            clicked_tag: clickable.tagName || "",
            clicked_text: norm(clickable.innerText || clickable.textContent || clickable.getAttribute("aria-label") || "").slice(0, 160),
            x: rect.left + rect.width / 2,
            y: rect.top + rect.height / 2
          };
        }
        """
        try:
            result = await asyncio.wait_for(self._maybe_await(scope.evaluate(script, selector)), timeout=timeout_ms / 1000)
        except asyncio.TimeoutError as exc:
            self._progress(
                "browser_join_click_failed",
                {"selector": selector, "scope": scope_name, "text": text, "method": "js_candidate_click", "reason": "timeout", "error": repr(exc)},
            )
            return None
        except Exception as exc:
            self._append_browser_log("browser_join_candidate_js_click_unavailable", repr(exc))
            self._progress(
                "browser_join_click_failed",
                {"selector": selector, "scope": scope_name, "text": text, "method": "js_candidate_click", "reason": "error", "error": repr(exc)},
            )
            return None
        if isinstance(result, Mapping) and result.get("ok"):
            return {**result, "selector": selector, "scope": scope_name, "text": text, "method": result.get("method") or "js_candidate_click"}
        self._progress(
            "browser_join_click_failed",
            {
                "selector": selector,
                "scope": scope_name,
                "text": text,
                "method": "js_candidate_click",
                "reason": (result or {}).get("reason") if isinstance(result, Mapping) else "not_clicked",
            },
        )
        return None

    async def _click_selector_in_scope(self, scope_name, scope, selector, timeout_ms):
        try:
            locator = scope.locator(selector).first
            await locator.wait_for(state="visible", timeout=timeout_ms)
            if hasattr(locator, "is_enabled") and not await self._text_action_locator_enabled(locator, timeout=timeout_ms):
                return None
            try:
                await locator.click(timeout=timeout_ms)
            except TypeError:
                await locator.click()
            return {"ok": True, "selector": selector, "scope": scope_name, "method": "scoped_visible_click"}
        except PlaywrightTimeoutError:
            return None
        except self._safe_playwright_errors() as exc:
            self._last_page_metadata_error = exc
            self._append_browser_log("browser_join_scoped_click_unavailable", repr(exc))
            return None
        except Exception as exc:
            self._append_browser_log("browser_join_scoped_click_unavailable", repr(exc))
            return None

    async def _raise_browser_join_click_not_found(self, state):
        extra = {
            "download_retry": state,
            "visible_text": await self._visible_text_excerpt(),
            "url": self._safe_page_url(),
            "title": await self._safe_page_title(),
            "links_buttons_debug": await self._links_buttons_debug_info(),
        }
        self._progress("browser_join_click_timeout", extra)
        diagnostics = await self._maybe_await(
            self.collect_diagnostics(stage="browser_join_click_not_found", extra=extra)
        )
        raise RuntimeError(f"browser_join_click_not_found. Diagnostics: {diagnostics}")

    async def _raise_webclient_frame_not_ready(self, state):
        frame = state.get("webclient_frame") or {}
        extra = {
            "download_retry": state,
            "webclient_frame": frame,
            "url": self._safe_page_url(),
            "title": await self._safe_page_title(),
            "visible_text": await self._visible_text_excerpt(),
        }
        self._progress("webex_webclient_frame_not_ready", extra)
        diagnostics = await self._maybe_await(
            self.collect_diagnostics(stage="webex_webclient_frame_not_ready", extra=extra)
        )
        raise RuntimeError(f"webex_webclient_frame_not_ready. Diagnostics: {diagnostics}")

    async def _click_final_join_control(self, display_name):
        if not self._is_page_available():
            raise RuntimeError("Webex page closed before final join control could be clicked")
        timeout = min(
            self.timeout_ms("optional_selector_timeout_ms", 1000),
            self.timeout_ms("final_join_candidate_timeout_ms", 150),
        )
        self._progress("final_join_click_attempt")
        for group in ("final_join_fast", "start_meeting_button", "join_button"):
            candidates = await self._visible_candidates(group, timeout_ms=timeout, include_locator=True)
            enabled = [item for item in candidates if item.get("enabled", True)]
            if enabled:
                selector = enabled[0]["selector"]
                if not self._is_page_available():
                    raise RuntimeError("Webex page closed before final join control could be clicked")
                await self._fill_display_name_if_needed(display_name, timeout_ms=timeout)
                await enabled[0]["_locator"].click()
                await self._dismiss_external_protocol_prompt(stage=f"after_click_{group}")
                return selector
            if candidates:
                fill_result = await self._fill_display_name_if_needed(display_name, timeout_ms=timeout)
                await asyncio.sleep(float(self.adapter_config().get("join_button_enable_wait_sec", 0.5)))
                candidates = await self._visible_candidates(group, timeout_ms=timeout, include_locator=True)
                enabled = [item for item in candidates if item.get("enabled", True)]
                if enabled:
                    selector = enabled[0]["selector"]
                    if not self._is_page_available():
                        raise RuntimeError("Webex page closed before final join control could be clicked")
                    await self._fill_display_name_if_needed(display_name, timeout_ms=timeout)
                    await enabled[0]["_locator"].click()
                    await self._dismiss_external_protocol_prompt(stage=f"after_click_{group}")
                    return selector
                diagnostic_candidates = [
                    {key: value for key, value in item.items() if key != "_locator"}
                    for item in candidates
                ]
                visible_text = await self._visible_text_excerpt()
                input_debug = await self._input_debug_info()
                self._progress(
                    "final_join_button_disabled",
                    {
                        "selector_group": group,
                        "candidates": diagnostic_candidates,
                        "visible_text": visible_text,
                        "input_debug": input_debug,
                    },
                )
                diagnostics = await self._maybe_await(
                    self.collect_diagnostics(
                        stage="webex_join_button_disabled",
                        extra={
                            "selector_group": group,
                            "candidates": diagnostic_candidates,
                            "visible_text": visible_text,
                            "display_name_fill": fill_result,
                            "input_debug": input_debug,
                        },
                    )
                )
                raise RuntimeError(
                    f"Webex final join button is visible but disabled. Visible text: {visible_text}. "
                    f"Input debug: {input_debug}. Diagnostics: {diagnostics}"
                )

        selector = await self._click_first_visible(
            "join_button",
            required=True,
            timeout_ms=self.timeout_ms("prejoin_timeout_ms", 30000),
        )
        await self._dismiss_external_protocol_prompt(stage="after_click_join_button")
        return selector

    async def _click_first_visible(self, selector_group, required=False, timeout_ms=None):
        found = await self._first_visible_locator(selector_group, timeout_ms=timeout_ms)
        if not found:
            if required:
                if not self._is_page_available():
                    raise RuntimeError(f"Webex page is unavailable while waiting for selector group: {selector_group}")
                raise RuntimeError(f"No visible selector matched: {self.selectors(selector_group)}")
            return None
        if not self._is_page_available():
            if required:
                raise RuntimeError(f"Webex page is unavailable while clicking selector group: {selector_group}")
            return None
        await found["locator"].click()
        return found["selector"]

    async def _fill_first_visible(self, selector_group, value, timeout_ms=None):
        if value in (None, ""):
            return None
        if not self._is_page_available():
            return None
        found = await self._first_visible_locator(selector_group, timeout_ms=timeout_ms)
        if not found:
            return None
        if not self._is_page_available():
            return None
        locator = found["locator"]
        if await self._fill_locator_verified(locator, value, timeout_ms=timeout_ms):
            return found["selector"]
        return None

    async def _fill_display_name_if_needed(self, display_name, timeout_ms=None, diagnose=False):
        joined_state = await self._scan_joined_state_all_surfaces(timeout_ms=min(50, int(timeout_ms or 50)))
        if self._is_terminal_join_state(joined_state):
            self._progress("display_name_fill_skipped", {"reason": "joined_state_detected", "status": joined_state["status"]})
            return {"success": False, "selector": None, "skipped": True, "join_state": joined_state}
        result = await self._fill_display_name(display_name, timeout_ms=timeout_ms)
        if result:
            return {"success": True, "selector": result}
        if diagnose:
            details = {
                "display_name": str(display_name or self._display_name()),
                "visible_text": await self._visible_text_excerpt(),
                "input_debug": await self._input_debug_info(),
                "fill_attempts": list(self.browser_log[-20:]),
            }
            await self._maybe_await(self.collect_diagnostics(stage="webex_name_fill_failed", extra=details))
        return {"success": False, "selector": None}

    async def _fill_display_name(self, display_name, timeout_ms=None):
        total_timeout_ms = self.timeout_ms("display_name_fill_total_timeout_ms", 8000)
        if timeout_ms is not None:
            total_timeout_ms = min(int(total_timeout_ms), max(1, int(timeout_ms) * 10))
        try:
            return await asyncio.wait_for(
                self._fill_display_name_bounded(display_name, timeout_ms=timeout_ms),
                timeout=max(0.001, total_timeout_ms / 1000),
            )
        except asyncio.TimeoutError:
            self._progress("webex_display_name_fill_timeout", {"timeout_ms": total_timeout_ms})
            return None

    async def _fill_display_name_bounded(self, display_name, timeout_ms=None):
        display_name = str(display_name or self._display_name())
        self._last_display_name_fill_method = None
        frame_candidate = self._select_live_webclient_frame_candidate()
        if frame_candidate:
            self._progress(
                "webex_display_name_frame_selected",
                {key: frame_candidate.get(key) for key in ("scope", "url", "name", "score")},
            )
        input_debug = await self._input_debug_info()
        frame_attempt_key = None
        if frame_candidate:
            frame_attempt_key = (frame_candidate.get("scope"), frame_candidate.get("url"), frame_candidate.get("name"))
            if input_debug.get("visible_text_input_count", 0) == 0 and frame_attempt_key in self._empty_display_name_frame_attempts:
                self._progress(
                    "display_name_fill_skipped",
                    {
                        "reason": "empty_frame_already_attempted",
                        "webclient_frame": {key: frame_candidate.get(key) for key in ("scope", "url", "name", "score")},
                    },
                )
                return None
        self._progress(
            "display_name_fill_attempt",
            {
                "display_name_length": len(display_name),
                "visible_text_input_count": input_debug.get("visible_text_input_count", 0),
            },
        )
        strong_guest_frame = bool(frame_candidate and self._is_strong_webex_guest_join_frame_url(frame_candidate.get("url")))
        if input_debug.get("visible_text_input_count", 0) == 0 and not strong_guest_frame:
            self._progress(
                "webex_display_name_input_not_found",
                {"reason": "no_visible_text_input", "strong_guest_frame": False},
            )
        if input_debug.get("visible_text_input_count", 0) == 0:
            if frame_attempt_key:
                self._empty_display_name_frame_attempts.add(frame_attempt_key)
            self._progress(
                "webex_wait_for_display_name_input_start",
                {"strong_guest_frame": strong_guest_frame, "timeout_ms": timeout_ms},
            )
        found = await self._find_display_name_input(timeout_ms=timeout_ms)
        if found:
            self._progress(
                "display_name_input_candidate_found",
                {
                    "selector": found["selector"],
                    "scope": found.get("scope"),
                    "strategy": found.get("strategy"),
                },
            )
            verified_method = await self._fill_locator_verified(found["locator"], display_name, timeout_ms=timeout_ms)
            if verified_method:
                emit_event(
                    self.config,
                    "webex_display_name_filled",
                    {"selector": found["selector"], "scope": found.get("scope"), "strategy": found.get("strategy")},
                    self.service_name,
                )
                self._progress(
                    "display_name_fill_verified",
                    {
                        "selector": found["selector"],
                        "scope": found.get("scope"),
                        "strategy": found.get("strategy"),
                        "fill_method": verified_method,
                    },
                )
                self._progress(
                    "display_name_fill_success",
                    {
                        "selector": found["selector"],
                        "scope": found.get("scope"),
                        "strategy": found.get("strategy"),
                        "fill_method": verified_method,
                        "verification_result": True,
                        "visible_text_input_count": input_debug.get("visible_text_input_count", 0),
                    },
                )
                self._progress(
                    "webex_display_name_fill_success",
                    {
                        "selector": found["selector"],
                        "scope": found.get("scope"),
                        "strategy": found.get("strategy"),
                        "fill_method": verified_method,
                    },
                )
                return found["selector"]
            self._progress(
                "display_name_fill_failed",
                {
                    "selector": found["selector"],
                    "scope": found.get("scope"),
                    "strategy": found.get("strategy"),
                    "fill_method": self._last_display_name_fill_method,
                    "verification_result": False,
                    "visible_text_input_count": input_debug.get("visible_text_input_count", 0),
                },
            )
            return None
        details = {
            "selector": None,
            "fill_method": None,
            "verification_result": False,
            "visible_text_input_count": input_debug.get("visible_text_input_count", 0),
            "webclient_frame": {key: frame_candidate.get(key) for key in ("scope", "url", "name", "score")} if frame_candidate else None,
        }
        self._progress("display_name_fill_failed", details)
        self._progress("webex_display_name_input_not_found", details)
        if strong_guest_frame:
            await self._maybe_await(
                self.collect_diagnostics(stage="webex_display_name_input_not_found", extra=details)
            )
            raise RuntimeError("webex_display_name_input_not_found")
        return None

    async def _name_entry_text_visible(self):
        text = await self._visible_text_excerpt(max_chars=4000)
        return "이름을 입력하고 참여하십시오" in text or "이름 *" in text

    async def _find_display_name_input(self, timeout_ms=None):
        timeout = self.timeout_ms("optional_selector_timeout_ms", 1000) if timeout_ms is None else timeout_ms
        candidate_timeout = min(int(timeout), int(self.adapter_config().get("display_name_candidate_timeout_ms", 200)))

        self._progress("webex_display_name_input_probe_start", {"timeout_ms": candidate_timeout})
        found = await self._find_display_name_input_by_selectors(timeout_ms=candidate_timeout)
        if found:
            self._progress(
                "webex_display_name_input_probe_result",
                {"found": True, "selector": found["selector"], "scope": found.get("scope"), "strategy": found.get("strategy")},
            )
            return found

        found = await self._find_single_visible_text_like_input(timeout_ms=candidate_timeout)
        if found:
            self._progress(
                "webex_display_name_input_probe_result",
                {"found": True, "selector": found["selector"], "scope": found.get("scope"), "strategy": found.get("strategy")},
            )
            return found

        found = await self._find_active_text_like_input(timeout_ms=candidate_timeout)
        if found:
            self._progress(
                "webex_display_name_input_probe_result",
                {"found": True, "selector": found["selector"], "scope": found.get("scope"), "strategy": found.get("strategy")},
            )
            return found

        label_patterns = (
            r"이름\s*\*?",
            r"Name",
            r"Your name",
            r"Display name",
        )
        for scope_name, scope in self._display_name_locator_scopes():
            get_by_label = getattr(scope, "get_by_label", None)
            if not get_by_label:
                continue
            for pattern in label_patterns:
                try:
                    locator = get_by_label(re.compile(pattern, re.IGNORECASE)).first
                    await locator.wait_for(state="visible", timeout=candidate_timeout)
                    return {
                        "selector": f"label=/{pattern}/i",
                        "locator": locator,
                        "scope": scope_name,
                        "strategy": "label",
                    }
                except PlaywrightTimeoutError:
                    continue
                except self._safe_playwright_errors() as exc:
                    self._last_page_metadata_error = exc
                    self._append_browser_log("display_name_label_unavailable", repr(exc))
                    continue
                except Exception:
                    continue

        role_patterns = (
            r"이름\s*\*?",
            r"Name",
            r"Your name",
            r"Display name",
        )
        for scope_name, scope in self._display_name_locator_scopes():
            get_by_role = getattr(scope, "get_by_role", None)
            if not get_by_role:
                continue
            role_attempts = [None, *role_patterns]
            for pattern in role_attempts:
                try:
                    if pattern is None:
                        locator = get_by_role("textbox").first
                        selector = "role=textbox"
                    else:
                        locator = get_by_role("textbox", name=re.compile(pattern, re.IGNORECASE)).first
                        selector = f"role=textbox[name=/{pattern}/i]"
                    await locator.wait_for(state="visible", timeout=candidate_timeout)
                    return {
                        "selector": selector,
                        "locator": locator,
                        "scope": scope_name,
                        "strategy": "role_textbox",
                    }
                except PlaywrightTimeoutError:
                    continue
                except self._safe_playwright_errors() as exc:
                    self._last_page_metadata_error = exc
                    self._append_browser_log("display_name_role_textbox_unavailable", repr(exc))
                    continue
                except Exception:
                    continue

        for selector in (
            'label:has-text("이름") >> xpath=following::input[1]',
            'text="이름" >> xpath=following::input[1]',
        ):
            for scope_name, scope in self._display_name_locator_scopes():
                try:
                    locator = scope.locator(selector).first
                    await locator.wait_for(state="visible", timeout=candidate_timeout)
                    return {"selector": selector, "locator": locator, "scope": scope_name, "strategy": "label_following_input"}
                except PlaywrightTimeoutError:
                    continue
                except self._safe_playwright_errors() as exc:
                    self._last_page_metadata_error = exc
                    self._append_browser_log("display_name_label_following_unavailable", repr(exc))
                    continue
                except Exception:
                    continue

        found = await self._find_active_text_like_input(timeout_ms=timeout)
        if found:
            self._progress(
                "webex_display_name_input_probe_result",
                {"found": True, "selector": found["selector"], "scope": found.get("scope"), "strategy": found.get("strategy")},
            )
            return found

        found = await self._find_single_visible_text_like_input(timeout_ms=timeout)
        self._progress(
            "webex_display_name_input_probe_result",
            {"found": bool(found), "selector": found.get("selector") if found else None, "scope": found.get("scope") if found else None, "strategy": found.get("strategy") if found else None},
        )
        return found

    def _display_name_input_selectors(self):
        selectors = []
        for selector in (
            'mdc-input input:not([type="hidden"])',
            'mdc-input textarea',
            'input[type="text"]',
            'input:not([type])',
            'textarea',
            '[contenteditable="true"]',
            'input:not([type="hidden"])[aria-label*="display name" i]',
            'input:not([type="hidden"])[aria-label*="your name" i]',
            'input:not([type="hidden"])[aria-label*="name" i]',
            'input:not([type="hidden"])[aria-label*="이름" i]',
            'input:not([type="hidden"])[name*="display name" i]',
            'input:not([type="hidden"])[name*="display" i]',
            'input:not([type="hidden"])[name*="name" i]',
            'input:not([type="hidden"])[name*="이름" i]',
            'input:not([type="hidden"])[placeholder*="display name" i]',
            'input:not([type="hidden"])[placeholder*="your name" i]',
            'input:not([type="hidden"])[placeholder*="name" i]',
            'input:not([type="hidden"])[placeholder*="이름" i]',
        ):
            if selector not in selectors:
                selectors.append(selector)
        for group in ("display_name", "name_input"):
            for selector in self.selectors(group):
                if selector not in selectors:
                    selectors.append(selector)
        return selectors

    def _display_name_locator_scopes(self):
        scopes = self._page_locator_scopes(prefer_meeting_frame=True)
        preferred = self._preferred_webex_meeting_frame
        if preferred is None:
            return scopes
        preferred_items = [item for item in scopes if item[1] is preferred]
        other_items = [item for item in scopes if item[1] is not preferred]
        return preferred_items + other_items

    async def _find_display_name_input_by_selectors(self, timeout_ms=None):
        timeout = self.timeout_ms("optional_selector_timeout_ms", 1000) if timeout_ms is None else timeout_ms
        for scope_name, scope in self._display_name_locator_scopes():
            for selector in self._display_name_input_selectors():
                try:
                    locator = scope.locator(selector).first
                    await locator.wait_for(state="visible", timeout=timeout)
                    return {
                        "selector": selector,
                        "locator": locator,
                        "scope": scope_name,
                        "strategy": "display_name_selector_probe",
                    }
                except PlaywrightTimeoutError:
                    continue
                except self._safe_playwright_errors() as exc:
                    self._last_page_metadata_error = exc
                    self._append_browser_log("display_name_selector_probe_unavailable", repr(exc))
                    continue
                except Exception:
                    continue
        return None

    async def _find_active_text_like_input(self, timeout_ms=None):
        if not self._is_page_available():
            return None
        timeout = self.timeout_ms("optional_selector_timeout_ms", 1000) if timeout_ms is None else timeout_ms
        selector = ':focus:is(input:not([type]), input[type="text"], input[type="search"], textarea, [role="textbox"], [contenteditable="true"])'
        for scope_name, scope in self._display_name_locator_scopes():
            try:
                locator = scope.locator(selector).first
                await locator.wait_for(state="visible", timeout=timeout)
                return {
                    "selector": selector,
                    "locator": locator,
                    "scope": scope_name,
                    "strategy": "active_focused_text_like_input",
                }
            except PlaywrightTimeoutError:
                continue
            except self._safe_playwright_errors() as exc:
                self._last_page_metadata_error = exc
                self._append_browser_log("active_text_input_unavailable", repr(exc))
                continue
            except Exception:
                continue
        return None

    async def _fill_single_visible_text_input(self, value, timeout_ms=None):
        found = await self._find_single_visible_text_like_input(timeout_ms=timeout_ms)
        if not found:
            return None
        if await self._fill_locator_verified(found["locator"], value, timeout_ms=timeout_ms):
            emit_event(self.config, "webex_display_name_filled", {"selector": found["selector"]}, self.service_name)
            return found["selector"]
        return None

    async def _find_single_visible_text_like_input(self, timeout_ms=None):
        if not self._is_page_available():
            return None
        timeout = self.timeout_ms("optional_selector_timeout_ms", 1000) if timeout_ms is None else timeout_ms
        text_like_selector = (
            'input:not([type]), input[type="text"], input[type="search"], '
            'textarea, [role="textbox"], [contenteditable="true"]'
        )
        for _scope_name, scope in self._display_name_locator_scopes():
            inputs = scope.locator(text_like_selector)
            visible = []
            try:
                if hasattr(inputs, "count"):
                    count = await self._maybe_await(inputs.count())
                    for index in range(count):
                        locator = inputs.nth(index) if hasattr(inputs, "nth") else inputs.first
                        try:
                            await locator.wait_for(state="visible", timeout=timeout)
                            visible.append(locator)
                        except PlaywrightTimeoutError:
                            continue
                else:
                    locator = inputs.first
                    try:
                        await locator.wait_for(state="visible", timeout=timeout)
                        visible.append(locator)
                    except PlaywrightTimeoutError:
                        continue
                if len(visible) == 1:
                    return {
                        "selector": text_like_selector,
                        "locator": visible[0],
                        "scope": _scope_name,
                        "strategy": "single_visible_text_like_input",
                    }
            except PlaywrightTimeoutError:
                continue
            except self._safe_playwright_errors() as exc:
                self._last_page_metadata_error = exc
                self._append_browser_log("single_text_input_unavailable", repr(exc))
                return None
        return None

    async def _fill_locator_verified(self, locator, value, timeout_ms=None):
        value = str(value)
        timeout = self.timeout_ms("optional_selector_timeout_ms", 1000) if timeout_ms is None else timeout_ms
        methods = (
            ("fill", self._fill_locator_with_fill),
            ("keyboard_select_type", self._fill_locator_with_keyboard_select_type),
            ("js_value_events", self._fill_locator_with_js_value_events),
            ("focused_keyboard_type", self._fill_locator_with_focused_keyboard_type),
        )
        for method_name, method in methods:
            try:
                self._progress("display_name_fill_method", {"method": method_name})
                await method(locator, value, timeout)
                if await self._locator_value_matches(locator, value, timeout):
                    self._last_display_name_fill_method = method_name
                    self._append_browser_log(
                        "webex_display_name_fill_verified",
                        json.dumps({"method": method_name, "value_length": len(value)}, default=str),
                    )
                    return method_name
            except PlaywrightTimeoutError:
                continue
            except self._safe_playwright_errors() as exc:
                self._last_page_metadata_error = exc
                self._append_browser_log("webex_display_name_fill_method_failed", f"{method_name}: {exc!r}")
                continue
            except Exception as exc:
                self._append_browser_log("webex_display_name_fill_method_failed", f"{method_name}: {exc!r}")
                continue
        return False

    async def _fill_locator_with_fill(self, locator, value, timeout):
        await locator.wait_for(state="visible", timeout=timeout)
        try:
            await locator.fill(value, timeout=timeout)
        except TypeError:
            await locator.fill(value)

    async def _fill_locator_with_keyboard_select_type(self, locator, value, timeout):
        await locator.wait_for(state="visible", timeout=timeout)
        if hasattr(locator, "click"):
            await self._click_locator_force(locator)
        keyboard = getattr(self.page, "keyboard", None)
        if keyboard is None:
            return
        shortcut = "Meta+A" if platform.system() == "Darwin" else "Control+A"
        if hasattr(keyboard, "press"):
            await self._maybe_await(keyboard.press(shortcut))
            await self._maybe_await(keyboard.press("Backspace"))
        if hasattr(keyboard, "type"):
            await self._maybe_await(keyboard.type(value))

    async def _fill_locator_with_js_value_events(self, locator, value, timeout):
        await locator.wait_for(state="visible", timeout=timeout)
        if hasattr(locator, "evaluate"):
            await self._maybe_await(
                locator.evaluate(
                    """(element, value) => {
                        const prototype =
                            element instanceof HTMLInputElement ? HTMLInputElement.prototype :
                            element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype :
                            element.constructor?.prototype;
                        const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
                        if (setter) {
                            setter.call(element, value);
                        } else if ('value' in element) {
                            element.value = value;
                        } else {
                            element.textContent = value;
                        }
                        const InputEventCtor = window.InputEvent || Event;
                        element.dispatchEvent(new InputEventCtor('input', {
                            bubbles: true,
                            inputType: 'insertText',
                            data: value
                        }));
                        element.dispatchEvent(new Event('change', { bubbles: true }));
                        element.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
                        return element.value || element.textContent || '';
                    }""",
                    value,
                )
            )

    async def _fill_locator_with_focused_keyboard_type(self, locator, value, timeout):
        await locator.wait_for(state="visible", timeout=timeout)
        if hasattr(locator, "click"):
            await self._click_locator_force(locator)
        keyboard = getattr(self.page, "keyboard", None)
        if keyboard is not None and hasattr(keyboard, "press"):
            shortcut = "Meta+A" if platform.system() == "Darwin" else "Control+A"
            await self._maybe_await(keyboard.press(shortcut))
            await self._maybe_await(keyboard.press("Backspace"))
        if keyboard is not None and hasattr(keyboard, "type"):
            await self._maybe_await(keyboard.type(value))

    async def _click_locator_force(self, locator):
        try:
            await locator.click(force=True, timeout=self.timeout_ms("optional_selector_timeout_ms", 1000))
        except TypeError:
            await locator.click()

    async def _locator_value_matches(self, locator, expected, timeout):
        actual = None
        if hasattr(locator, "input_value"):
            try:
                actual = await self._maybe_await(locator.input_value(timeout=timeout))
            except TypeError:
                actual = await self._maybe_await(locator.input_value())
            except Exception:
                actual = None
        if actual is None and hasattr(locator, "evaluate"):
            try:
                actual = await self._maybe_await(
                    locator.evaluate("(element) => element.value || element.textContent || ''")
                )
            except Exception:
                actual = None
        return str(actual or "") == str(expected)

    async def _input_debug_info(self):
        info = {
            "visible_text_input_count": 0,
            "inputs": [],
            "active_element": {},
        }
        if not self._is_page_available():
            return info
        selector = 'input:not([type="hidden"]), textarea, [contenteditable="true"]'
        for scope_name, scope in self._page_locator_scopes():
            frame_info = {
                "scope": scope_name,
                "url": str(getattr(scope, "url", "") or ""),
                "name": "",
            }
            try:
                frame_name = getattr(scope, "name", "")
                frame_info["name"] = frame_name() if callable(frame_name) else str(frame_name or "")
            except Exception:
                frame_info["name"] = ""
            try:
                inputs = scope.locator(selector)
                count = await self._maybe_await(inputs.count()) if hasattr(inputs, "count") else 1
                for index in range(count):
                    locator = inputs.nth(index) if hasattr(inputs, "nth") else inputs.first
                    try:
                        await locator.wait_for(state="visible", timeout=100)
                    except Exception:
                        continue
                    item = dict(frame_info)
                    item["index"] = index
                    if hasattr(locator, "evaluate"):
                        try:
                            attrs = await self._maybe_await(
                                locator.evaluate(
                                    """(element) => ({
                                        tagName: element.tagName || '',
                                        class: element.getAttribute('class') || '',
                                        name: element.getAttribute('name') || '',
                                        id: element.getAttribute('id') || '',
                                        type: element.getAttribute('type') || '',
                                        placeholder: element.getAttribute('placeholder') || '',
                                        aria_label: element.getAttribute('aria-label') || '',
                                        role: element.getAttribute('role') || '',
                                        value: element.value || element.textContent || '',
                                        focused: element === document.activeElement
                                    })"""
                                )
                            )
                            if isinstance(attrs, Mapping):
                                item.update(attrs)
                        except Exception as exc:
                            item["error"] = repr(exc)
                    if "value" not in item and hasattr(locator, "input_value"):
                        try:
                            item["value"] = await self._maybe_await(locator.input_value(timeout=100))
                        except Exception:
                            item["value"] = ""
                    if hasattr(locator, "is_enabled"):
                        try:
                            item["enabled"] = bool(await self._maybe_await(locator.is_enabled(timeout=100)))
                        except TypeError:
                            item["enabled"] = bool(await self._maybe_await(locator.is_enabled()))
                        except Exception:
                            item["enabled"] = None
                    if hasattr(locator, "is_editable"):
                        try:
                            item["editable"] = bool(await self._maybe_await(locator.is_editable(timeout=100)))
                        except TypeError:
                            item["editable"] = bool(await self._maybe_await(locator.is_editable()))
                        except Exception:
                            item["editable"] = None
                    if hasattr(locator, "bounding_box"):
                        try:
                            item["bounding_box"] = await self._maybe_await(locator.bounding_box(timeout=100))
                        except TypeError:
                            item["bounding_box"] = await self._maybe_await(locator.bounding_box())
                        except Exception:
                            item["bounding_box"] = None
                    item["visible"] = True
                    info["inputs"].append(item)
                    input_type = str(item.get("type") or "text").lower()
                    if input_type in {"", "text", "search"} or item.get("role") == "textbox":
                        info["visible_text_input_count"] += 1
            except Exception as exc:
                info.setdefault("errors", []).append({"scope": scope_name, "error": repr(exc)})
            try:
                if hasattr(scope, "evaluate"):
                    active = await self._maybe_await(
                        scope.evaluate(
                            """() => {
                            const element = document.activeElement;
                            if (!element) return {};
                            return {
                                tagName: element.tagName || '',
                                tag: element.tagName || '',
                                class: element.getAttribute('class') || '',
                                name: element.getAttribute('name') || '',
                                id: element.getAttribute('id') || '',
                                type: element.getAttribute('type') || '',
                                placeholder: element.getAttribute('placeholder') || '',
                                aria_label: element.getAttribute('aria-label') || '',
                                role: element.getAttribute('role') || '',
                                value: element.value || element.textContent || ''
                            };
                        }"""
                        )
                    )
                    if isinstance(active, Mapping):
                        active = {**frame_info, **active}
                        info.setdefault("active_elements", []).append(active)
                        if not info["active_element"]:
                            info["active_element"] = active
            except Exception as exc:
                info.setdefault("active_elements", []).append({**frame_info, "error": repr(exc)})
        return info

    async def _debug_visible_text_inputs(self):
        return await self._input_debug_info()

    async def _dismiss_external_protocol_prompt(self, stage=None):
        adapter_config = self.adapter_config()
        if not bool(adapter_config.get("dismiss_external_protocol_dialog", True)):
            return False

        self._progress("external_protocol_dismiss_attempt", {"trigger_stage": stage})
        method = str(adapter_config.get("external_protocol_dismiss_method", "auto") or "auto").lower()
        strict = bool(adapter_config.get("strict_external_protocol_dismiss", False))
        attempts = []

        async def record(command, action, runner=None):
            if runner is None:
                runner = self._run_external_protocol_command
            try:
                result = runner(command, action)
                if hasattr(result, "__await__"):
                    result = await result
            except Exception as exc:
                result = {"command": command, "action": action, "success": False, "error": repr(exc)}
            attempts.append(result)
            return bool(result.get("success"))

        if self._is_page_available():
            keyboard = getattr(self.page, "keyboard", None)
            press = getattr(keyboard, "press", None)
            if press and method in {"auto", "escape", "keyboard"}:
                try:
                    await self._maybe_await(press("Escape"))
                    attempts.append({"action": "page_keyboard_escape", "command": ["page.keyboard.press", "Escape"], "success": True})
                except Exception as exc:
                    attempts.append({"action": "page_keyboard_escape", "command": ["page.keyboard.press", "Escape"], "success": False, "error": repr(exc)})

        system = platform.system()
        if method in {"auto", "osascript", "system"} and system == "Darwin":
            for action, script in self._macos_external_protocol_dismiss_scripts():
                await record(["osascript", "-e", script], action)

        if method in {"auto", "xdotool", "system"} and system == "Linux" and shutil.which("xdotool"):
            display = self._configured_display()
            if shutil.which("wmctrl"):
                await record(["wmctrl", "-a", "Webex"], "external_protocol_prompt_linux_activate_webex")
                await record(["wmctrl", "-a", "Chrome"], "external_protocol_prompt_linux_activate_chrome")
            await record(["xdotool", "key", "Escape"], "external_protocol_prompt_linux_escape", runner=lambda command, action: self._run_external_protocol_command(command, action, env_display=display))

        if method in {"auto", "powershell", "system"} and system == "Windows":
            await record(
                ["powershell", "-NoProfile", "-Command", "$wshell = New-Object -ComObject wscript.shell; $wshell.SendKeys('{ESC}')"],
                "external_protocol_prompt_windows_escape",
            )

        if attempts:
            self.external_protocol_dismiss_attempts.extend(attempts)
            self._append_browser_log("webex_external_protocol_prompt_dismiss_attempted", json.dumps({"stage": stage, "attempts": attempts}, default=str))
            emit_event(
                self.config,
                "webex_external_protocol_prompt_dismiss_attempted",
                {"stage": stage, "attempts": attempts},
                self.service_name,
            )

        permission_issue = self._external_protocol_permission_issue(attempts)
        if permission_issue:
            details = {
                "stage": stage,
                "attempts": attempts,
                "permission_issue": permission_issue,
                "message": (
                    "macOS blocked osascript/System Events while dismissing the Webex external protocol dialog. "
                    "Grant Automation/Accessibility permission to the terminal or Python process running this smoke test."
                ),
            }
            self._append_browser_log("webex_external_protocol_prompt_permission_blocked", json.dumps(details, default=str))
            emit_event(self.config, "webex_external_protocol_prompt_permission_blocked", details, self.service_name)
            if strict:
                raise RuntimeError(
                    "Webex external protocol prompt dismissal is blocked by macOS Automation/Accessibility permission: "
                    f"{permission_issue}"
                )

        success = any(item.get("success") for item in attempts)
        self._progress("external_protocol_dismiss_done", {"trigger_stage": stage, "success": success, "attempt_count": len(attempts)})
        if not success:
            details = {"stage": stage, "attempts": attempts}
            self._append_browser_log("webex_external_protocol_prompt_dismiss_failed", json.dumps(details, default=str))
            emit_event(self.config, "webex_external_protocol_prompt_dismiss_failed", details, self.service_name)
            if strict:
                diagnostics = await self._maybe_await(
                    self.collect_diagnostics(stage="webex_external_protocol_prompt_blocking", extra=details)
                )
                raise RuntimeError(f"Webex external protocol prompt could not be dismissed. Diagnostics: {diagnostics}")
        return success

    def _macos_external_protocol_dismiss_scripts(self):
        process_names = ("Google Chrome for Testing", "Google Chrome", "Chromium")
        scripts = []

        for process_name in process_names:
            scripts.append(
                (
                    f"external_protocol_prompt_macos_recursive_cancel_{process_name}",
                    f'''
tell application "System Events"
    if exists process "{process_name}" then
        tell process "{process_name}"
            set frontmost to true
        end tell
        key code 53
        delay 0.2
        tell process "{process_name}"
            repeat with candidateWindow in windows
                set clickResult to my clickCancelButton(candidateWindow)
                if clickResult starts with "clicked:" then return clickResult
            end repeat
        end tell
        return "sent_escape"
    end if
end tell
return "process_not_found:{process_name}"

on cancelNames()
    return {{"취소", "Cancel", "Not Now"}}
end cancelNames

on elementLabel(uiElement)
    try
        set candidateName to name of uiElement as text
        if candidateName is not "" then return candidateName
    end try
    try
        set candidateTitle to title of uiElement as text
        if candidateTitle is not "" then return candidateTitle
    end try
    return ""
end elementLabel

on isCheckBox(uiElement)
    try
        if role of uiElement as text is "AXCheckBox" then return true
    end try
    try
        if role description of uiElement as text contains "checkbox" then return true
    end try
    try
        if class of uiElement is checkbox then return true
    end try
    return false
end isCheckBox

on isButton(uiElement)
    try
        if role of uiElement as text is "AXButton" then return true
    end try
    try
        if role description of uiElement as text contains "button" then return true
    end try
    try
        if class of uiElement is button then return true
    end try
    return false
end isButton

on clickCancelButton(uiElement)
    if my isCheckBox(uiElement) then return "not_found"
    if my isButton(uiElement) then
        set candidateName to my elementLabel(uiElement)
        if candidateName is in my cancelNames() then
            click uiElement
            return "clicked: " & candidateName
        end if
    end if
    try
        repeat with childElement in UI elements of uiElement
            set childResult to my clickCancelButton(childElement)
            if childResult starts with "clicked:" then return childResult
        end repeat
    end try
    return "not_found"
end clickCancelButton
'''.strip(),
                )
            )

        scripts.append(
            (
                "external_protocol_prompt_macos_escape_final",
                '''
tell application "System Events"
    key code 53
end tell
return "sent_escape"
'''.strip(),
            )
        )
        return scripts

    def _safe_playwright_errors(self):
        errors = []
        for cls in (TargetClosedError, PlaywrightError):
            if isinstance(cls, type) and cls not in errors:
                errors.append(cls)
        return tuple(errors) or (Exception,)

    def _is_transient_frame_transition_error(self, exc):
        text = f"{exc!r} {exc}".lower()
        return any(
            phrase in text
            for phrase in (
                "frame was detached",
                "target closed",
                "execution context was destroyed",
            )
        )

    def _is_target_closed_error(self, exc):
        return isinstance(exc, self._safe_playwright_errors()) and "closed" in repr(exc).lower()

    def _page_metadata_saw_target_closed(self):
        return self._last_page_metadata_error is not None and self._is_target_closed_error(self._last_page_metadata_error)

    def _is_page_available(self):
        page = self.page
        if page is None:
            return False
        try:
            is_closed = getattr(page, "is_closed", None)
            if callable(is_closed):
                return not bool(is_closed())
            return True
        except self._safe_playwright_errors() as exc:
            self._last_page_metadata_error = exc
            self._append_browser_log("page_availability_failed", repr(exc))
            return False
        except Exception as exc:
            self._last_page_metadata_error = exc
            self._append_browser_log("page_availability_failed", repr(exc))
            return False

    async def _safe_page_title(self, default=""):
        if not self._is_page_available():
            return default
        try:
            self._last_page_metadata_error = None
            return await self._maybe_await(self.page.title())
        except self._safe_playwright_errors() as exc:
            self._last_page_metadata_error = exc
            self._append_browser_log("page_title_unavailable", repr(exc))
            return default
        except Exception as exc:
            self._last_page_metadata_error = exc
            self._append_browser_log("page_title_unavailable", repr(exc))
            return default

    def _safe_page_url(self, default=""):
        if not self._is_page_available():
            return default
        try:
            return getattr(self.page, "url", default) or default
        except self._safe_playwright_errors() as exc:
            self._last_page_metadata_error = exc
            self._append_browser_log("page_url_unavailable", repr(exc))
            return default
        except Exception as exc:
            self._last_page_metadata_error = exc
            self._append_browser_log("page_url_unavailable", repr(exc))
            return default

    async def _safe_page_content(self, default=""):
        if not self._is_page_available():
            return default
        try:
            return await self._maybe_await(self.page.content())
        except self._safe_playwright_errors() as exc:
            self._last_page_metadata_error = exc
            self._append_browser_log("page_content_unavailable", repr(exc))
            return default
        except Exception as exc:
            self._last_page_metadata_error = exc
            self._append_browser_log("page_content_unavailable", repr(exc))
            return default

    async def _safe_screenshot(self, default=None, **kwargs):
        if not self._is_page_available():
            return default
        try:
            return await self._maybe_await(self.page.screenshot(**kwargs))
        except self._safe_playwright_errors() as exc:
            self._last_page_metadata_error = exc
            self._append_browser_log("page_screenshot_unavailable", repr(exc))
            return default
        except Exception as exc:
            self._last_page_metadata_error = exc
            self._append_browser_log("page_screenshot_unavailable", repr(exc))
            return default

    async def _safe_visible_text_excerpt(self, max_chars=2000, default=""):
        if not self._is_page_available():
            return default
        text = ""
        for _scope_name, scope in self._page_locator_scopes():
            for selector in ("body", "html"):
                if not self._is_page_available():
                    return default
                try:
                    locator = scope.locator(selector).first
                    if hasattr(locator, "inner_text"):
                        text = await self._maybe_await(locator.inner_text(timeout=1000))
                    elif hasattr(locator, "text_content"):
                        text = await self._maybe_await(locator.text_content(timeout=1000))
                    if text:
                        break
                except self._safe_playwright_errors() as exc:
                    self._last_page_metadata_error = exc
                    self._append_browser_log("page_visible_text_unavailable", repr(exc))
                    return default
                except Exception as exc:
                    self._last_page_metadata_error = exc
                    self._append_browser_log("page_visible_text_unavailable", repr(exc))
                    continue
            if text:
                break
        text = " ".join(str(text or "").split())
        if text:
            return text[:max_chars]
        fallback = await self._html_text_excerpt(max_chars=max_chars, default=default)
        if fallback:
            self._progress("webex_page_text_fallback_used", {"max_chars": max_chars})
        return fallback

    async def _html_text_excerpt(self, max_chars=2000, default=""):
        html_text = await self._safe_page_content(default="")
        if not html_text:
            return default
        text = self._sanitize_html_text(html_text)
        return text[:max_chars] if text else default

    def _sanitize_html_text(self, html_text):
        text = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", str(html_text or ""))
        text = re.sub(r"(?is)<br\s*/?>", " ", text)
        text = re.sub(r"(?is)</(p|div|li|h[1-6]|button|a|span)>", " ", text)
        text = re.sub(r"(?is)<[^>]+>", " ", text)
        return " ".join(html.unescape(text).split())

    async def _visible_text_excerpt(self, max_chars=2000):
        return await self._safe_visible_text_excerpt(max_chars=max_chars)

    async def _wait_for_join_result(self, timeout_sec):
        deadline = asyncio.get_running_loop().time() + max(0, float(timeout_sec))
        poll_timeout_ms = self.timeout_ms("join_result_poll_timeout_ms", 500)
        while asyncio.get_running_loop().time() <= deadline:
            joined_state = await self._scan_joined_state_all_surfaces(timeout_ms=poll_timeout_ms)
            if self._is_terminal_join_state(joined_state):
                return joined_state
            if await self._title_indicates_joined():
                return {
                    "status": "joined",
                    "joined": True,
                    "joined_source": "title",
                    "selector": "title:joined",
                    "joined_selector": "title:joined",
                    "visible_text": await self._visible_text_excerpt(),
                }
            for status, group in (
                ("waiting_for_others", "waiting_for_others_indicator"),
                ("joined", "joined_indicator"),
                ("lobby", "lobby_indicator"),
                ("blocked", "blocked_indicator"),
            ):
                selector = await self._first_visible_selector(group, timeout_ms=poll_timeout_ms)
                if selector:
                    joined_selector = None
                    if status == "waiting_for_others":
                        joined_selector = await self._first_visible_selector(
                            "joined_indicator",
                            timeout_ms=self.timeout_ms("join_result_secondary_poll_timeout_ms", 50),
                        )
                    return {
                        "status": status,
                        "selector": selector,
                        "joined_selector": joined_selector,
                        "visible_text": await self._visible_text_excerpt(),
                    }
            await asyncio.sleep(float(self.adapter_config().get("join_result_poll_interval_sec", 0.25)))

        return {
            "status": "timeout",
            "selector": None,
            "visible_text": await self._visible_text_excerpt(),
        }

    async def _wait_for_post_final_join_result(self, timeout_sec):
        timeout_sec = max(0.1, float(timeout_sec))
        deadline = asyncio.get_running_loop().time() + timeout_sec
        poll_timeout_ms = self.timeout_ms("join_result_poll_timeout_ms", 500)
        self._progress(
            "webex_post_final_join_wait_start",
            {"timeout_sec": timeout_sec, "final_join_clicked": bool(getattr(self, "_final_join_clicked_success", False))},
        )
        while asyncio.get_running_loop().time() <= deadline:
            state = await self._scan_joined_state_all_surfaces(timeout_ms=poll_timeout_ms)
            state = self._post_final_join_state_from_scan(state)
            if state["status"] == "continue":
                selector_state = await self._post_final_join_selector_state(timeout_ms=poll_timeout_ms)
                if selector_state["status"] != "continue":
                    state = selector_state
            elif state["status"] in {"waiting_for_others", "waiting_for_host"} and not state.get("joined_selector"):
                state["joined_selector"] = await self._first_visible_selector(
                    "joined_indicator",
                    timeout_ms=self.timeout_ms("join_result_secondary_poll_timeout_ms", 50),
                )
            self._progress("webex_post_final_join_scan_result", state)
            if state["status"] in {"joined", "waiting_for_others", "waiting_for_host", "lobby", "join_error", "blocked"}:
                self._progress("webex_join_result_detected", state)
                return state
            await asyncio.sleep(float(self.adapter_config().get("join_result_poll_interval_sec", 0.25)))

        diagnostics = await self._post_final_join_timeout_diagnostics()
        stage = self._post_final_join_timeout_stage(diagnostics)
        result = {
            "status": "timeout",
            "stage": stage,
            "selector": None,
            "visible_text": diagnostics.get("visible_text", ""),
            "source": "post_final_join",
            "final_join_clicked": bool(getattr(self, "_final_join_clicked_success", False)),
            "diagnostics": diagnostics,
        }
        self._progress(stage, result)
        return result

    def _post_final_join_state_from_scan(self, state):
        state = self._normalize_join_state(state or {"status": "continue", "selector": None, "visible_text": ""})
        if not isinstance(state, Mapping):
            return {"status": "continue", "selector": None, "visible_text": "", "source": "post_final_join"}
        state = dict(state)
        state["source"] = "post_final_join"
        if state.get("selector") == "window_fallback":
            state = self._post_final_join_window_fallback_state(state)
        return state

    def _post_final_join_window_fallback_state(self, state):
        state = dict(state)
        window_text = str(state.get("window") or "")
        page_title = str(state.get("title") or "")
        candidate = {
            **state,
            "page_title": page_title,
            "window": window_text,
            "candidate_reason": self._post_final_join_title_classification(window_text, page_title),
        }
        self._progress("webex_post_final_join_window_fallback_candidate", candidate)

        if self._post_final_join_title_is_joined(window_text) or self._post_final_join_title_is_joined(page_title):
            state.update(
                {
                    "status": "joined",
                    "joined": True,
                    "joined_source": "post_final_join_window_fallback",
                    "joined_selector": "window_fallback",
                    "selector": "window_fallback",
                }
            )
            self._progress("webex_post_final_join_window_fallback_success", state)
            return state

        reason = candidate["candidate_reason"]
        rejected = {
            **state,
            "status": "continue",
            "joined": False,
            "joined_source": None,
            "joined_selector": None,
            "selector": "window_fallback",
            "reason": reason,
        }
        self._progress("webex_post_final_join_window_fallback_rejected", rejected)
        return rejected

    def _post_final_join_title_classification(self, *texts):
        combined = " ".join(str(text or "") for text in texts)
        lowered = " ".join(combined.split()).lower()
        if not lowered:
            return "not_joined_yet"
        if "get ready to join" in lowered:
            return "post_final_join_pending"
        if "join from this browser" in lowered or "problem joining from browser" in lowered:
            return "prejoin_still_visible"
        if "loading" in lowered or "download" in lowered or "cisco webex" in lowered:
            return "not_joined_yet"
        return "not_joined_yet"

    def _post_final_join_title_is_joined(self, text):
        normalized = " ".join(str(text or "").split()).lower()
        return bool(
            "미팅 중" in normalized
            or "in meeting" in normalized
            or "meeting in progress" in normalized
            or "in meeting · meeting · webex" in normalized
        )

    async def _post_final_join_selector_state(self, timeout_ms=None):
        for status, group in (
            ("waiting_for_others", "waiting_for_others_indicator"),
            ("joined", "joined_indicator"),
            ("lobby", "lobby_indicator"),
            ("blocked", "blocked_indicator"),
        ):
            selector = await self._first_visible_selector(group, timeout_ms=timeout_ms)
            if selector:
                return {
                    "status": status,
                    "joined": status in {"joined", "waiting_for_others"},
                    "selector": selector,
                    "joined_selector": selector if status in {"joined", "waiting_for_others"} else None,
                    "visible_text": await self._visible_text_excerpt(),
                    "source": "post_final_join",
                }
        return {"status": "continue", "selector": None, "visible_text": "", "source": "post_final_join"}

    async def _post_final_join_timeout_diagnostics(self):
        visible_text = await self._visible_text_excerpt()
        hidden_text = await self._html_text_excerpt()
        diagnostics = {
            "final_join_clicked": bool(getattr(self, "_final_join_clicked_success", False)),
            "page_url": self._safe_page_url(),
            "page_title": await self._safe_page_title(default=""),
            "context_page_urls": await self._context_page_urls(),
            "window_list": self._webex_window_list(),
            "visible_text": visible_text[:2000],
            "hidden_text": hidden_text[:2000],
        }
        return diagnostics

    def _post_final_join_timeout_stage(self, diagnostics):
        texts = [
            diagnostics.get("page_title", ""),
            diagnostics.get("visible_text", ""),
            diagnostics.get("hidden_text", ""),
            " ".join(diagnostics.get("window_list") or []),
        ]
        classification = self._post_final_join_title_classification(*texts)
        if classification == "post_final_join_pending":
            return "webex_post_final_join_pending_timeout"
        return "webex_post_final_join_result_timeout"

    def _prejoin_timeout_stage(self, title="", visible_text="", window_list=None, download_retry=None):
        if isinstance(download_retry, Mapping) and download_retry.get("detected"):
            return "webex_download_retry_page_timeout"
        combined = " ".join([str(title or ""), str(visible_text or ""), " ".join(window_list or [])])
        lowered = " ".join(combined.split()).lower()
        if "loading" in lowered or "cisco webex" in lowered:
            return "webex_prejoin_loading_timeout"
        return "webex_no_actionable_state_timeout"

    async def _context_page_urls(self):
        pages = [self.page]
        if self.context is not None:
            pages_attr = getattr(self.context, "pages", None)
            if pages_attr is not None:
                try:
                    pages = list(pages_attr() if callable(pages_attr) else pages_attr)
                except Exception:
                    pages = [self.page]
        urls = []
        for page in pages:
            if page is None:
                continue
            try:
                urls.append(str(getattr(page, "url", "") or ""))
            except Exception:
                urls.append("")
        return urls

    def _webex_window_list(self):
        if platform.system() != "Linux" or not shutil.which("wmctrl"):
            return []
        display = self._configured_display()
        env = dict(os.environ)
        if display:
            env["DISPLAY"] = display
        try:
            result = subprocess.run(["wmctrl", "-lG"], capture_output=True, text=True, timeout=2, check=False, env=env)
        except Exception as exc:
            self._append_browser_log("webex_window_list_unavailable", repr(exc))
            return []
        if result.returncode != 0:
            return []
        return [line for line in (result.stdout or "").splitlines() if line.strip()]

    async def _title_indicates_joined(self):
        title = str(await self._safe_page_title(default="") or "")
        normalized = " ".join(title.split()).lower()
        return any(
            token in normalized
            for token in (
                "미팅 중",
                "in meeting",
                "meeting in progress",
            )
        )

    async def _fallback_action(self, action_name):
        fallback = self.adapter_config().get("fallback", {})
        if not isinstance(fallback, Mapping) or not (
            fallback.get("enabled", False)
            or self.adapter_config().get("allow_unverified_fallback", False)
            or fallback.get("allow_unverified_fallback", False)
        ):
            return False

        key = fallback.get(f"{action_name}_key")
        if key:
            return self._run_command(["xdotool", "key", str(key)], action_name)

        coordinates = fallback.get("coordinates", {})
        point = coordinates.get(action_name) if isinstance(coordinates, Mapping) else None
        if point and len(point) == 2:
            return self._run_command(["xdotool", "mousemove", str(point[0]), str(point[1]), "click", "1"], action_name)

        return False

    def _run_command(self, command, action_name):
        result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
        emit_event(
            self.config,
            "webex_fallback_action",
            {
                "action": action_name,
                "command": command,
                "returncode": result.returncode,
                "success": result.returncode == 0,
                "stderr": result.stderr.strip(),
            },
            self.service_name,
        )
        return result.returncode == 0

    def _run_external_protocol_command(self, command, action_name, env_display=None):
        env = None
        if env_display:
            env = dict(os.environ)
            env["DISPLAY"] = env_display
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=3, check=False, env=env)
            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()
            success = result.returncode == 0
            if command and command[0] == "osascript" and stdout.startswith(("not_found:", "process_not_found:")):
                success = False
            payload = {
                "action": action_name,
                "command": command,
                "returncode": result.returncode,
                "success": success,
                "stdout": stdout,
                "stderr": stderr,
            }
        except Exception as exc:
            payload = {
                "action": action_name,
                "command": command,
                "success": False,
                "error": repr(exc),
            }
        return payload

    def _external_protocol_permission_issue(self, attempts):
        permission_tokens = (
            "not allowed assistive access",
            "not authorized to send apple events",
            "not permitted to send keystrokes",
            "is not allowed to send keystrokes",
            "not allowed to control system events",
            "access for assistive devices is disabled",
            "(-1743)",
            "(-25211)",
        )
        for attempt in attempts:
            command = attempt.get("command") or []
            if not command or command[0] != "osascript":
                continue
            text = " ".join(
                str(attempt.get(key) or "")
                for key in ("stdout", "stderr", "error")
            )
            lowered = text.lower()
            if any(token in lowered for token in permission_tokens):
                return text.strip() or repr(attempt)
        return None

    def _has_browser_arg(self, args, desired):
        if "=" in desired:
            desired_key = desired.split("=", 1)[0]
            return any(str(arg) == desired or str(arg).startswith(f"{desired_key}=") for arg in args)
        return desired in args

    def _configured_display(self):
        return str(self.adapter_config().get("display") or self.config.get("display") or os.environ.get("DISPLAY") or "")

    def _discover_browser_executable(self):
        for binary in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            path = shutil.which(binary)
            if path:
                return path
        return None

    def _run_sanity_command(self, command, env_display=None):
        env = None
        if env_display:
            env = dict(os.environ)
            env["DISPLAY"] = env_display
        try:
            return subprocess.run(command, capture_output=True, text=True, timeout=5, check=False, env=env)
        except Exception as exc:
            return subprocess.CompletedProcess(command, returncode=127, stdout="", stderr=repr(exc))

    def _diagnostic_dir(self):
        adapter_config = self.adapter_config()
        configured = (
            adapter_config.get("diagnostic_dir")
            or adapter_config.get("diagnostics_dir")
            or self.config.get("diagnostic_dir")
            or self.config.get("diagnostics_dir")
        )
        return Path(str(configured or "artifacts/webex_diagnostics")).expanduser()

    def _diagnostic_commands(self):
        return {
            "wmctrl_windows": ["wmctrl", "-lG"],
            "xdotool_visible_windows": ["xdotool", "search", "--onlyvisible", "--name", "."],
            "pactl_info": ["pactl", "info"],
            "pactl_sources": ["pactl", "list", "short", "sources"],
            "pactl_source_outputs": ["pactl", "list", "source-outputs"],
            "video_devices": ["/bin/bash", "-lc", "ls -l /dev/video*"],
            "processes": ["ps", "-ef"],
        }

    def _safe_adapter_config(self):
        safe_keys = {
            "browser_channel",
            "browser_executable_path",
            "chromium_executable_path",
            "display",
            "headless",
            "ignore_certificate_errors",
            "disable_gpu",
            "screen_share_target",
            "auto_select_desktop_capture_source",
            "initial_microphone_enabled",
            "initial_camera_enabled",
            "initial_screen_sharing",
            "apply_initial_media_state_in_prejoin",
            "strict_prejoin_media_state",
            "fail_on_prejoin_media_state_unverified",
            "dismiss_external_protocol_dialog",
            "external_protocol_dismiss_method",
            "strict_external_protocol_dismiss",
            "suppress_external_protocol_dialog",
            "use_persistent_browser_profile_for_webex",
            "chrome_user_data_dir",
            "blocked_external_protocol_schemes",
            "protocol_handler_excluded_scheme_value",
            "verify_joined",
            "prejoin_timeout_ms",
            "joined_timeout_ms",
            "optional_selector_timeout_ms",
            "control_state_timeout_ms",
            "control_state_settle_sec",
            "trust_click_state_after_unverified_action",
            "allow_unverified_fallback",
            "skip_sanity_checks",
            "strict_sanity_checks",
            "diagnostic_dir",
            "diagnostics_dir",
            "retry_navigation_on_page_closed",
            "max_navigation_retries",
            "max_download_retry_attempts",
            "download_retry_settle_ms",
            "download_retry_click_total_timeout_sec",
            "permissions",
            "viewport",
        }
        return {key: value for key, value in self.adapter_config().items() if key in safe_keys}

    async def _maybe_await(self, value):
        if hasattr(value, "__await__"):
            return await value
        return value

    def _attach_page_logging(self, page):
        if page is None:
            return
        try:
            page.on("console", lambda msg: self._record_browser_event("console", msg))
            page.on("pageerror", lambda exc: self._record_browser_event("pageerror", exc))
            page.on("requestfailed", lambda request: self._record_browser_event("requestfailed", request))
            page.on("close", lambda: self._append_browser_log("page_closed", "page close event"))
        except Exception as exc:
            self._append_browser_log("logging_error", repr(exc))

    def _attach_browser_logging(self, browser):
        if browser is None:
            return
        try:
            browser.on("disconnected", lambda: self._append_browser_log("browser_disconnected", "browser disconnected event"))
        except Exception as exc:
            self._append_browser_log("logging_error", repr(exc))

    def _attach_context_logging(self, context):
        if context is None:
            return
        try:
            context.on("close", lambda: self._append_browser_log("context_closed", "context close event"))
        except Exception as exc:
            self._append_browser_log("logging_error", repr(exc))

    def _record_browser_event(self, event_type, obj):
        try:
            if event_type == "console":
                message = self._read_attr(obj, "text") or str(obj)
                location = self._read_attr(obj, "location")
                level = self._read_attr(obj, "type")
                self._append_browser_log(event_type, message, location=location, level=level)
            elif event_type == "requestfailed":
                failure = self._read_attr(obj, "failure")
                message = f"{self._read_attr(obj, 'url') or ''} {failure or ''}".strip()
                self._append_browser_log(event_type, message)
            else:
                self._append_browser_log(event_type, str(obj))
        except Exception as exc:
            self._append_browser_log("logging_error", repr(exc))

    def _read_attr(self, obj, name):
        value = getattr(obj, name, None)
        if callable(value):
            try:
                return value()
            except TypeError:
                return None
        return value

    def _append_browser_log(self, event_type, message, location=None, level=None):
        self.browser_log.append(
            {
                "ts": utc_now_iso(),
                "event_type": event_type,
                "message": str(message),
                "location": location,
                "level": level,
            }
        )
        if len(self.browser_log) > 1000:
            self.browser_log = self.browser_log[-1000:]

    def _flush_browser_log(self):
        log_path = self.adapter_config().get("browser_log_path") or self.config.get("browser_log_path")
        if not log_path:
            return None
        try:
            path = Path(str(log_path)).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.browser_log, indent=2, sort_keys=True, default=str), encoding="utf-8")
            return str(path)
        except Exception:
            return None

    def _display_name(self):
        bot = self.config.get("bot")
        if isinstance(bot, Mapping) and bot.get("display_name"):
            return str(bot["display_name"])
        return str(self.adapter_config().get("display_name") or self.config.get("bot_name") or "bot")

    def _notify_meeting_joined(self, vtc_url):
        if self._meeting_joined_notified:
            return
        self._meeting_joined_notified = True
        emit_event(self.config, "meeting_joined", {"vtc_url": vtc_url}, self.service_name)
        callback = self.config.get("_meeting_joined_callback")
        if callback:
            callback(vtc_url)

    def _notify_media_ready(self, vtc_url):
        if self._media_ready_notified:
            return
        self._media_ready_notified = True
        emit_event(self.config, "media_ready", {"vtc_url": vtc_url}, self.service_name)
        callback = self.config.get("_media_ready_callback")
        if callback:
            callback(vtc_url)
