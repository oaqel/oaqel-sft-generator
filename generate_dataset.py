#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════╗
║   مولّد داتا التدريب — النسخة الاحترافية v2.0                      ║
║   50 موضوع | 5 قطاعات | توليد متوازٍ حقيقي | GitHub Actions جاهز  ║
║   دعم checkpoint في Releases + تخطّي حد 6 ساعات تلقائياً           ║
╚══════════════════════════════════════════════════════════════════════╝

الاستخدام:
  python generate_dataset.py                   # تشغيل عادي
  python generate_dataset.py --retry-failed    # إعادة المهام الفاشلة
  python generate_dataset.py --stats           # إحصاء ملف موجود فقط

متغيرات البيئة المدعومة:
  GEMINI_API_KEYS   — مفاتيح مفصولة بفواصل  (إجباري)
  SAMPLES_PER_TOPIC — عدد العينات لكل موضوع (افتراضي: 10)
  MODEL_ID          — نموذج Gemini          (افتراضي: gemini-2.0-flash)
  OUTPUT_FILE       — مسار ملف الإخراج      (افتراضي: training_dataset.json)
  DELAY_PER_KEY     — ثواني بين كل call/key  (افتراضي: 1.5)
  FORCE_RESTART     — ابدأ من الصفر          (افتراضي: false)
  MAX_RUNTIME_SEC   — حد زمني بالثواني       (افتراضي: 19800 = 5.5 ساعة)
"""

from __future__ import annotations

import json
import logging
import os
import queue
import random
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────────────────────────────
#  🪵  إعداد الـ Logger
# ─────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("generator.log", encoding="utf-8", mode="a"),
    ],
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
#  ⚙️  الإعدادات — تُقرأ من متغيرات البيئة أو القيم الافتراضية
# ─────────────────────────────────────────────────────────────────────

def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default

def _env_bool(key: str, default: bool = False) -> bool:
    return os.environ.get(key, str(default)).lower() in ("1", "true", "yes")

def _env_list(key: str, default: list[str]) -> list[str]:
    raw = os.environ.get(key, "")
    if raw:
        return [k.strip() for k in raw.split(",") if k.strip()]
    return default


# ── القيم الافتراضية ───────────────────────────────────────────────

GEMINI_API_KEYS: list[str] = _env_list("GEMINI_API_KEYS", [
    "YOUR_GEMINI_API_KEY_1",
    "YOUR_GEMINI_API_KEY_2",
    "YOUR_GEMINI_API_KEY_3",
])

MODEL_ID            = _env("MODEL_ID",          "gemini-2.0-flash")
OUTPUT_FILE         = _env("OUTPUT_FILE",        "training_dataset.json")
CHECKPOINT_FILE     = _env("CHECKPOINT_FILE",    "checkpoint.json")
FAILED_FILE         = _env("FAILED_FILE",        "failed_tasks.json")
RESTART_SIGNAL_FILE = "needs_restart.flag"       # يقرأه الـ YML

SAMPLES_PER_TOPIC   = _env_int("SAMPLES_PER_TOPIC",  10)
DELAY_PER_KEY       = float(_env("DELAY_PER_KEY",    "1.5"))   # ثانية بين كل call/مفتاح
SAVE_EVERY          = _env_int("SAVE_EVERY",          10)       # حفظ كل N عينة
MAX_RETRIES         = _env_int("MAX_RETRIES",          3)
FORCE_RESTART       = _env_bool("FORCE_RESTART",       False)
MAX_RUNTIME_SEC     = _env_int("MAX_RUNTIME_SEC", 5 * 3600 + 30 * 60)  # 5.5 ساعة

# ميزانية الكلمات لكل حقل
WORD_BUDGETS: dict[str, int] = {
    "system" : 35,
    "query"  : 90,
    "thought": 320,
    "answer" : 400,
}


# ─────────────────────────────────────────────────────────────────────
#  📋  الـ 50 موضوع — 5 قطاعات × 10 مواضيع
# ─────────────────────────────────────────────────────────────────────

TOPICS: list[dict] = [

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  قطاع 1: التفاعل الاجتماعي والأدب (يمنع الموديل من الرد بالرياضة لـ "السلام عليكم")
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {"id":  1, "sector": "التفاعل الاجتماعي",
     "topic": "التحية والترحيب بمختلف الصيغ الفصحى"},
    {"id":  2, "sector": "التفاعل الاجتماعي",
     "topic": "التعريف بالنفس — من أنت وما دورك كمساعد ذكي"},
    {"id":  3, "sector": "التفاعل الاجتماعي",
     "topic": "الاعتذار عند الخطأ أو عدم المعرفة بأسلوب أنيق"},
    {"id":  4, "sector": "التفاعل الاجتماعي",
     "topic": "الشكر والرد على الثناء والإطراء"},
    {"id":  5, "sector": "التفاعل الاجتماعي",
     "topic": "إنهاء المحادثة وتوديع المستخدم بأسلوب دافئ"},
    {"id":  6, "sector": "التفاعل الاجتماعي",
     "topic": "إدارة الحوار اليومي — كيف حالك وماذا تريد"},
    {"id":  7, "sector": "التفاعل الاجتماعي",
     "topic": "التعامل الأدبي مع الإهانات والمحتوى غير اللائق"},
    {"id":  8, "sector": "التفاعل الاجتماعي",
     "topic": "التعبير عن رأي محايد في مسألة خلافية"},
    {"id":  9, "sector": "التفاعل الاجتماعي",
     "topic": "الاقتباسات والأقوال المأثورة وشرح دلالتها"},
    {"id": 10, "sector": "التفاعل الاجتماعي",
     "topic": "الأسئلة الوجودية البسيطة والإجابة عنها بعمق"},

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  قطاع 2: المهارات اللغوية والإبداعية
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {"id": 11, "sector": "المهارات اللغوية",
     "topic": "تصحيح الأخطاء النحوية والإملائية مع الشرح"},
    {"id": 12, "sector": "المهارات اللغوية",
     "topic": "إعادة صياغة الجمل بأسلوب أجزل وأرقى"},
    {"id": 13, "sector": "المهارات اللغوية",
     "topic": "كتابة قصة قصيرة إبداعية ذات بداية وعقدة ونهاية"},
    {"id": 14, "sector": "المهارات اللغوية",
     "topic": "كتابة الشعر العمودي والحر في مواضيع متنوعة"},
    {"id": 15, "sector": "المهارات اللغوية",
     "topic": "تلخيص نص طويل مع الحفاظ على المعنى الجوهري"},
    {"id": 16, "sector": "المهارات اللغوية",
     "topic": "شرح الأمثال العربية القديمة وتأويل سياقها"},
    {"id": 17, "sector": "المهارات اللغوية",
     "topic": "الترجمة بين العامية والفصحى مع مراعاة الفروق"},
    {"id": 18, "sector": "المهارات اللغوية",
     "topic": "كتابة رسائل رسمية وبريد إلكتروني احترافي"},
    {"id": 19, "sector": "المهارات اللغوية",
     "topic": "ابتكار ألغاز لغوية ومعمّيات ذكية"},
    {"id": 20, "sector": "المهارات اللغوية",
     "topic": "كتابة مقالات قصيرة مقنعة في مواضيع عامة"},

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  قطاع 3: المنطق والاستنتاج — لتقوية الـ Reasoning
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {"id": 21, "sector": "المنطق والاستنتاج",
     "topic": "حل مسائل رياضية خطوة بخطوة مع شرح المنطق"},
    {"id": 22, "sector": "المنطق والاستنتاج",
     "topic": "الألغاز المنطقية Logic Puzzles وإثبات الحل"},
    {"id": 23, "sector": "المنطق والاستنتاج",
     "topic": "المقارنة التحليلية بين خيارين وتقديم توصية مبررة"},
    {"id": 24, "sector": "المنطق والاستنتاج",
     "topic": "استخراج الأسباب والنتائج من نص قصير"},
    {"id": 25, "sector": "المنطق والاستنتاج",
     "topic": "ترتيب الأحداث زمنياً واستنتاج التسلسل المنطقي"},
    {"id": 26, "sector": "المنطق والاستنتاج",
     "topic": "التفكير النقدي في موضوع جدلي مع تقديم أدلة"},
    {"id": 27, "sector": "المنطق والاستنتاج",
     "topic": "أسئلة ماذا لو — تحليل سيناريوهات افتراضية"},
    {"id": 28, "sector": "المنطق والاستنتاج",
     "topic": "تبسيط المفاهيم المعقدة لمستويات مختلفة من الفهم"},
    {"id": 29, "sector": "المنطق والاستنتاج",
     "topic": "اتخاذ قرارات مدروسة بناءً على معطيات متضاربة"},
    {"id": 30, "sector": "المنطق والاستنتاج",
     "topic": "كشف التناقضات والمغالطات المنطقية في النصوص"},

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  قطاع 4: المعلومات العامة والمعرفة
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {"id": 31, "sector": "المعلومات العامة",
     "topic": "حقائق تاريخية مثيرة — عربية وعالمية"},
    {"id": 32, "sector": "المعلومات العامة",
     "topic": "معلومات جغرافية — دول وعواصم وأنهار وجبال"},
    {"id": 33, "sector": "المعلومات العامة",
     "topic": "أساسيات العلوم — فيزياء وكيمياء وأحياء بطريقة ممتعة"},
    {"id": 34, "sector": "المعلومات العامة",
     "topic": "التقنية والبرمجة — مفاهيم نظرية مبسطة"},
    {"id": 35, "sector": "المعلومات العامة",
     "topic": "الطب والصحة العامة — معلومات مفيدة مع إخلاء مسؤولية"},
    {"id": 36, "sector": "المعلومات العامة",
     "topic": "الفضاء والنجوم وعجائب الكون"},
    {"id": 37, "sector": "المعلومات العامة",
     "topic": "الثقافة الإسلامية — تاريخ وسير وأعلام"},
    {"id": 38, "sector": "المعلومات العامة",
     "topic": "الفنون والعمارة الإسلامية والعالمية"},
    {"id": 39, "sector": "المعلومات العامة",
     "topic": "الرياضة العالمية وقوانينها وأبرز بطولاتها"},
    {"id": 40, "sector": "المعلومات العامة",
     "topic": "البيئة والتغير المناخي وأثره على حياتنا"},

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  قطاع 5: المهام الوظيفية والـ Formatting
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    {"id": 41, "sector": "المهام الوظيفية",
     "topic": "تحويل نص مطوّل إلى نقاط مرتبة ومختصرة"},
    {"id": 42, "sector": "المهام الوظيفية",
     "topic": "استخراج الكلمات المفتاحية من نص عربي"},
    {"id": 43, "sector": "المهام الوظيفية",
     "topic": "تحويل المعلومات المتناثرة إلى جدول منظم"},
    {"id": 44, "sector": "المهام الوظيفية",
     "topic": "كتابة وصف تسويقي احترافي ومقنع لمنتج"},
    {"id": 45, "sector": "المهام الوظيفية",
     "topic": "صياغة أسئلة اختبار متنوعة من فقرة نصية"},
    {"id": 46, "sector": "المهام الوظيفية",
     "topic": "تقديم نصائح عملية منظمة ومرتبة بأولويات"},
    {"id": 47, "sector": "المهام الوظيفية",
     "topic": "كتابة سيناريوهات حوارية واقعية بين شخصين"},
    {"id": 48, "sector": "المهام الوظيفية",
     "topic": "استخراج الكيانات المسماة NER من نص غير منظم"},
    {"id": 49, "sector": "المهام الوظيفية",
     "topic": "كتابة تعليمات استخدام واضحة ومرتبة — User Manual"},
    {"id": 50, "sector": "المهام الوظيفية",
     "topic": "تصنيف النصوص وتحديد طبيعتها ومجالها الموضوعي"},
]

# ── توجيهات خاصة بكل قطاع تُحسّن جودة التوليد ─────────────────────

_SECTOR_HINTS: dict[str, str] = {
    "التفاعل الاجتماعي": (
        "الـ query يجب أن يكون رسالة طبيعية من مستخدم حقيقي. "
        "الـ answer يكون رداً دافئاً ومناسباً سياقياً. "
        "تجنب الردود الجافة أو الرسمية المبالغ فيها."
    ),
    "المهارات اللغوية": (
        "الـ query يجب أن يحتوي على نص عربي فعلي للتعامل معه. "
        "الـ answer يُظهر المخرج النهائي والشرح معاً. "
        "أضف ملاحظة لغوية إضافية تُثري المستخدم."
    ),
    "المنطق والاستنتاج": (
        "الـ thought يجب أن يكون تحليلاً تدريجياً واضحاً يُظهر مسار التفكير. "
        "الـ answer يقدم الحل مع التبرير الكامل، ليس مجرد النتيجة. "
        "استخدم الخطوات المرقمة عند الحل."
    ),
    "المعلومات العامة": (
        "الـ query يطرح سؤالاً معلوماتياً محدداً وعملياً. "
        "الـ answer غني بالحقائق والأرقام والسياق التاريخي. "
        "أضف معلومة مثيرة للاهتمام في النهاية."
    ),
    "المهام الوظيفية": (
        "الـ query يحتوي على نص مدخل فعلي ومهمة واضحة. "
        "الـ answer يُظهر الناتج المنسق بشكل احترافي. "
        "اجعل المخرجات مرتبة بصرياً باستخدام النقاط أو الجداول حسب المطلوب."
    ),
}


# ─────────────────────────────────────────────────────────────────────
#  🔑  إدارة مفاتيح API مع Rate Limiting لكل مفتاح
# ─────────────────────────────────────────────────────────────────────

class _KeySlot:
    """فتحة مفتاح API واحد مع قفل التزامن."""

    def __init__(self, key: str, min_delay: float):
        from google import genai as _genai
        self.key        = key
        self.client     = _genai.Client(api_key=key)
        self.min_delay  = min_delay
        self._lock      = threading.Lock()
        self._last_call = 0.0
        self.errors     = 0
        self.calls      = 0
        self.disabled   = False

    def wait_and_claim(self) -> bool:
        """ينتظر حتى يكون المفتاح جاهزاً ثم يحجزه. يعيد False لو معطوب."""
        if self.disabled:
            return False
        with self._lock:
            now  = time.time()
            wait = self.min_delay - (now - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.time()
            return True

    def report_error(self, msg: str):
        self.errors += 1
        is_rate = any(x in msg.lower() for x in ["quota", "rate", "429", "limit"])
        penalty = 45 if is_rate else 5
        self._last_call = time.time() + penalty  # عقوبة مؤقتة
        if self.errors >= 8:
            self.disabled = True
            log.warning(f"🚫 مفتاح معطوب بعد {self.errors} أخطاء")

    @property
    def index_label(self) -> str:
        return f"#{self.key[-6:]}"  # آخر 6 أحرف للتعريف الآمن


class KeyPool:
    """مجموعة مفاتيح API مع توزيع Round-Robin."""

    def __init__(self, keys: list[str], delay: float):
        valid = [k for k in keys if k and not k.startswith("YOUR_")]
        if not valid:
            raise ValueError("❌ لا توجد مفاتيح API صحيحة! عدّل GEMINI_API_KEYS.")
        self._slots    = [_KeySlot(k, delay) for k in valid]
        self._rr       = 0
        self._rr_lock  = threading.Lock()
        log.info(f"✅ {len(self._slots)} مفاتيح API جاهزة")

    def get_ready_slot(self) -> _KeySlot:
        """يُعيد أول فتحة جاهزة بنظام Round-Robin."""
        visited = 0
        while True:
            with self._rr_lock:
                active = [s for s in self._slots if not s.disabled]
                if not active:
                    raise RuntimeError("جميع مفاتيح API معطوبة!")
                slot = active[self._rr % len(active)]
                self._rr += 1

            if slot.wait_and_claim():
                return slot

            visited += 1
            if visited > len(self._slots) * 3:
                time.sleep(0.5)  # انتظار خفيف لو كل المفاتيح مشغولة

    @property
    def active_count(self) -> int:
        return sum(1 for s in self._slots if not s.disabled)

    def status_report(self) -> str:
        lines = []
        for i, s in enumerate(self._slots):
            st = "🚫 معطوب" if s.disabled else "✅ نشط"
            lines.append(f"  مفتاح #{i+1}: {s.calls} استدعاء | {s.errors} أخطاء | {st}")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
#  💾  مدير نقطة الاستئناف
# ─────────────────────────────────────────────────────────────────────

class CheckpointManager:
    def __init__(self, cp_path: str, out_path: str):
        self.cp_path  = Path(cp_path)
        self.out_path = Path(out_path)
        self._lock    = threading.Lock()

    def load(self) -> tuple[list[dict], set[str]]:
        dataset, completed = [], set()
        if self.out_path.exists():
            try:
                dataset = json.loads(self.out_path.read_text(encoding="utf-8"))
                log.info(f"📂 استُعيدت {len(dataset)} عينة من '{self.out_path}'")
            except Exception as e:
                log.warning(f"تعذّر تحميل ملف الإخراج: {e}")

        if self.cp_path.exists():
            try:
                cp = json.loads(self.cp_path.read_text(encoding="utf-8"))
                completed = set(cp.get("completed_keys", []))
                log.info(f"📌 {len(completed)} مهمة مكتملة في نقطة الاستئناف")
            except Exception as e:
                log.warning(f"تعذّر تحميل نقطة الاستئناف: {e}")

        return dataset, completed

    def save(self, dataset: list[dict], completed: set[str]):
        with self._lock:
            _atomic_write(self.out_path, dataset)
            _atomic_write(self.cp_path, {
                "completed_keys": list(completed),
                "count": len(dataset),
                "saved_at": datetime.now().isoformat(),
                "samples_per_topic": SAMPLES_PER_TOPIC,
            })


def _atomic_write(path: Path, data):
    """كتابة آمنة: اكتب لملف مؤقت أولاً ثم استبدل."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ─────────────────────────────────────────────────────────────────────
#  📝  بناء البرومبت الاحترافي
# ─────────────────────────────────────────────────────────────────────

_PROMPT_HEADER = """\
أنت خبير متخصص في إنشاء بيانات تدريب عالية الجودة للنماذج اللغوية العربية.
مهمتك هي توليد مثال تدريبي واحد متقن ودقيق.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  القطاع : {sector}
  الموضوع: {topic}
  العينة : {sample_num} من {total_samples} (اجعلها مختلفة تماماً عن العينات السابقة)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

⚙️  مواصفات الجودة الإلزامية:
• system  : رسالة نظام للنموذج — جملة مختصرة ≤ 35 كلمة
• query   : سؤال أو طلب واقعي من مستخدم حقيقي ≤ 90 كلمة
• thought : سلسلة تفكير داخلية تدريجية ≤ 320 كلمة — تبدأ حرفياً بـ: "باذن الله ساقوم بالتفكير بعمق."
• answer  : إجابة نافعة ومكتملة ≤ 500 كلمة — واضحة، منظمة، بلا حشو

🎯 توجيهات القطاع:
{sector_hint}

📌 معايير الجودة:
✓ اللغة العربية الفصحى السليمة دون أخطاء إملائية
✓ الـ query يحتوي على مشكلة أو طلب واقعي وملموس (وليس مجرد عنوان)
✓ الـ answer يقدم قيمة حقيقية، منظمة ومكتملة
✓ تنويع الأسلوب والمحتوى عن العينات السابقة لنفس الموضوع
✓ الـ thought يُظهر مسار التفكير الحقيقي خطوة بخطوة

⚠️  أعد JSON object واحداً فقط بدون أي markdown أو مقدمات أو شرح:
{{"system": "...", "query": "...", "thought": "...", "answer": "..."}}
"""


def build_prompt(topic_entry: dict, sample_num: int) -> str:
    sector = topic_entry["sector"]
    return _PROMPT_HEADER.format(
        sector       = sector,
        topic        = topic_entry["topic"],
        sample_num   = sample_num,
        total_samples= SAMPLES_PER_TOPIC,
        sector_hint  = _SECTOR_HINTS.get(sector, "اجعل المثال عملياً ومفيداً."),
    )


# ─────────────────────────────────────────────────────────────────────
#  ✅  تحقق من الجودة وتطبيع الحقول
# ─────────────────────────────────────────────────────────────────────

def _count_words(text: str) -> int:
    return len(text.split())

def _trim(text: str, max_w: int) -> str:
    words = text.split()
    if len(words) <= max_w:
        return text
    trimmed = " ".join(words[:max_w])
    if trimmed[-1] not in ".!?،؟":
        trimmed += "."
    return trimmed

def validate_and_fix(raw: dict, topic_entry: dict, sample_num: int) -> Optional[dict]:
    """يتحقق من البنية ويطبق الحدود ويضيف البيانات الوصفية."""
    required = ("system", "query", "thought", "answer")

    # التحقق من الحقول الأساسية
    for field in required:
        if field not in raw or not isinstance(raw[field], str) or not raw[field].strip():
            log.debug(f"حقل ناقص أو فارغ: {field}")
            return None

    # تطبيق حدود الكلمات
    for field, budget in WORD_BUDGETS.items():
        raw[field] = _trim(raw[field].strip(), budget)

    # إجبار البداية الصحيحة للـ thought
    thought = raw["thought"]
    prefix  = "باذن الله ساقوم بالتفكير بعمق."
    if not thought.startswith("باذن الله"):
        raw["thought"] = prefix + " " + thought

    # تحقق من الحد الأدنى للجودة
    if _count_words(raw["answer"]) < 20:
        log.debug("الإجابة قصيرة جداً — رفض")
        return None

    # إضافة البيانات الوصفية
    raw["_meta"] = {
        "topic_id"  : topic_entry["id"],
        "topic"     : topic_entry["topic"],
        "sector"    : topic_entry["sector"],
        "sample_num": sample_num,
        "words"     : {f: _count_words(raw[f]) for f in required},
        "generated_at": datetime.now().isoformat(),
    }
    return raw


def parse_response(text: str) -> Optional[dict]:
    """يستخرج JSON من رد الموديل."""
    clean = text.strip()

    # إزالة markdown code blocks
    for pat in (r"```json\s*(.*?)\s*```", r"```\s*(.*?)\s*```"):
        m = re.search(pat, clean, re.DOTALL)
        if m:
            clean = m.group(1)
            break

    # استخراج أول كائن JSON
    m = re.search(r'\{.*\}', clean, re.DOTALL)
    if not m:
        return None

    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return None


# ─────────────────────────────────────────────────────────────────────
#  ⚡  محرك التوليد المتوازي
# ─────────────────────────────────────────────────────────────────────

@dataclass
class _Stats:
    total     : int = 0
    done      : int = 0
    success   : int = 0
    failed    : int = 0
    lock      : threading.Lock = threading.Lock()

    def increment(self, success: bool):
        with self.lock:
            self.done    += 1
            if success:
                self.success += 1
            else:
                self.failed  += 1

    @property
    def progress_pct(self) -> float:
        return (self.done / self.total * 100) if self.total else 0.0


class ParallelGenerator:
    """محرك التوليد المتوازي — عامل لكل مفتاح API."""

    def __init__(
        self,
        key_pool   : KeyPool,
        cp_manager : CheckpointManager,
        stats      : _Stats,
    ):
        self.key_pool    = key_pool
        self.cp_manager  = cp_manager
        self.stats       = stats
        self._task_q     : queue.Queue[Optional[dict]] = queue.Queue()
        self._results    : list[dict]  = []
        self._failed     : list[dict]  = []
        self._completed  : set[str]    = set()
        self._res_lock   = threading.Lock()
        self._stop       = threading.Event()
        self._start_time = time.time()

    def _is_timeout(self) -> bool:
        return (time.time() - self._start_time) >= MAX_RUNTIME_SEC

    def _task_key(self, task: dict) -> str:
        te = task["topic_entry"]
        return f"{te['id']}_{task['sample_num']}"

    def _worker(self, worker_id: int):
        while not self._stop.is_set():
            try:
                task = self._task_q.get(timeout=1.0)
            except queue.Empty:
                break
            if task is None:  # poison pill
                break

            if self._is_timeout():
                # أعد المهمة للقائمة المعلقة (سيُرسَل بعد)
                with self._res_lock:
                    self._failed.append(task)
                self._task_q.task_done()
                self._stop.set()
                break

            result = self._generate_one(task, worker_id)
            key    = self._task_key(task)

            with self._res_lock:
                if result:
                    self._results.append(result)
                    self._completed.add(key)
                else:
                    self._failed.append(task)

            self.stats.increment(success=bool(result))
            self._task_q.task_done()

    def _generate_one(self, task: dict, worker_id: int) -> Optional[dict]:
        te  = task["topic_entry"]
        sn  = task["sample_num"]

        for attempt in range(1, MAX_RETRIES + 1):
            slot = self.key_pool.get_ready_slot()
            try:
                response = slot.client.models.generate_content(
                    model    = MODEL_ID,
                    contents = build_prompt(te, sn),
                )
                slot.calls += 1
                raw = parse_response(response.text)
                if raw is None:
                    raise ValueError("لم يُعثر على JSON صالح في الرد")

                example = validate_and_fix(raw, te, sn)
                if example is None:
                    raise ValueError("فشل التحقق من الجودة")

                total_w = sum(example["_meta"]["words"].values())
                log.info(
                    f"[W{worker_id}] ✅ #{te['id']:>2}·{sn}"
                    f" {te['sector']:<16}"
                    f" {te['topic'][:28]:<28}"
                    f" [{total_w}ك]"
                    f" [{self.stats.done+1}/{self.stats.total}]"
                )
                return example

            except Exception as exc:
                err = str(exc)
                slot.report_error(err)
                log.warning(f"[W{worker_id}] ⚠️ محاولة {attempt}/{MAX_RETRIES}: {err[:70]}")
                if attempt < MAX_RETRIES:
                    time.sleep(2 ** attempt)

        log.error(f"[W{worker_id}] ❌ فشل نهائي: #{te['id']}·{sn} — {te['topic'][:30]}")
        return None

    def run(
        self,
        tasks      : list[dict],
        existing   : list[dict],
        completed  : set[str],
    ) -> tuple[list[dict], list[dict], set[str], bool]:
        """
        يُشغّل العمال ويُعيد (نتائج_جديدة, فاشلة, مكتملة, يحتاج_إعادة_تشغيل).
        """
        self._results   = []
        self._failed    = []
        self._completed = set(completed)
        self._start_time= time.time()

        random.shuffle(tasks)  # عشوائية كاملة

        for t in tasks:
            self._task_q.put(t)

        n_workers = self.key_pool.active_count
        workers   = []
        for i in range(n_workers):
            t = threading.Thread(target=self._worker, args=(i + 1,), daemon=True)
            t.start()
            workers.append(t)

        # حفظ دوري في الخيط الرئيسي
        all_data = list(existing)
        save_counter = 0

        while any(w.is_alive() for w in workers):
            time.sleep(2)

            with self._res_lock:
                new_batch = self._results[:]
                done_keys  = set(self._completed)

            # حفظ دوري
            if len(new_batch) - save_counter >= SAVE_EVERY:
                all_data = list(existing) + new_batch
                self.cp_manager.save(all_data, done_keys)
                save_counter = len(new_batch)
                elapsed  = time.time() - self._start_time
                rate     = self.stats.success / (elapsed / 60) if elapsed > 0 else 0
                pct      = self.stats.progress_pct
                log.info(
                    f"💾 حفظ تلقائي — {len(all_data)} عينة | "
                    f"{pct:.1f}% | {rate:.1f}/دقيقة"
                )

        for w in workers:
            w.join()

        all_data  = list(existing) + self._results
        needs_restart = self._is_timeout() and bool(self._failed)
        return all_data, self._failed, self._completed, needs_restart


# ─────────────────────────────────────────────────────────────────────
#  📊  ملخص إحصائي
# ─────────────────────────────────────────────────────────────────────

def print_summary(dataset: list[dict]):
    if not dataset:
        print("\n(لا توجد عينات)")
        return

    sectors: dict[str, int] = {}
    word_counts = []

    for ex in dataset:
        meta  = ex.get("_meta", {})
        sec   = meta.get("sector", ex.get("_section", "غير محدد"))
        sectors[sec] = sectors.get(sec, 0) + 1

        wc = meta.get("words") or {
            f: _count_words(ex.get(f, ""))
            for f in ("system", "query", "thought", "answer")
        }
        word_counts.append(sum(wc.values()))

    avg_w = sum(word_counts) / len(word_counts) if word_counts else 0
    max_w = max(word_counts) if word_counts else 0
    over  = sum(1 for w in word_counts if w > sum(WORD_BUDGETS.values()))

    print(f"\n{'═'*62}")
    print(f"  ✅ بفضل الله — ملخص التوليد")
    print(f"{'═'*62}")
    print(f"  إجمالي العينات : {len(dataset)}")
    print(f"  متوسط الكلمات  : {avg_w:.0f}")
    print(f"  أعلى عدد كلمات : {max_w}")
    print(f"  تجاوز الحد     : {over} عينة")
    print(f"\n  توزيع القطاعات:")
    for sec, cnt in sorted(sectors.items(), key=lambda x: -x[1]):
        bar = "█" * (cnt // 2)
        print(f"    {sec:<22} {cnt:>4}  {bar}")
    print(f"\n  📁 الملف: '{OUTPUT_FILE}'")
    print(f"{'═'*62}")


# ─────────────────────────────────────────────────────────────────────
#  🚀  المحرك الرئيسي
# ─────────────────────────────────────────────────────────────────────

def run_generation():
    print("═" * 62)
    print("  مولّد داتا التدريب — النسخة الاحترافية v2.0")
    print("═" * 62)

    key_pool   = KeyPool(GEMINI_API_KEYS, DELAY_PER_KEY)
    cp_manager = CheckpointManager(CHECKPOINT_FILE, OUTPUT_FILE)

    # تحميل ما سبق إنجازه (أو بدء من الصفر)
    if FORCE_RESTART:
        log.info("⚡ FORCE_RESTART — بدء من الصفر")
        dataset, completed = [], set()
    else:
        dataset, completed = cp_manager.load()

    # بناء قائمة كل المهام
    all_tasks = [
        {"topic_entry": t, "sample_num": s}
        for t in TOPICS
        for s in range(1, SAMPLES_PER_TOPIC + 1)
    ]

    def task_key(t: dict) -> str:
        return f"{t['topic_entry']['id']}_{t['sample_num']}"

    pending = [t for t in all_tasks if task_key(t) not in completed]

    print(f"\n📊 إحصاء الجلسة:")
    print(f"   المهام الكلية  : {len(all_tasks)}")
    print(f"   مكتملة         : {len(all_tasks) - len(pending)}")
    print(f"   متبقية          : {len(pending)}")
    print(f"   مفاتيح API      : {key_pool.active_count}")
    print(f"   عينات/موضوع    : {SAMPLES_PER_TOPIC}")
    print(f"   حد زمني         : {MAX_RUNTIME_SEC // 3600:.1f} ساعة")

    if not pending:
        print("\n✅ كل المهام مكتملة بالفعل!")
        print_summary(dataset)
        return

    stats = _Stats(total=len(pending))
    gen   = ParallelGenerator(key_pool, cp_manager, stats)

    print(f"\n🚀 بدء التوليد المتوازي بـ {key_pool.active_count} عامل...\n")

    all_data, failed, completed_new, needs_restart = gen.run(
        tasks=pending, existing=dataset, completed=completed
    )

    # الحفظ النهائي
    merged_completed = completed | completed_new
    cp_manager.save(all_data, merged_completed)

    # حفظ الفاشلة
    if failed:
        _atomic_write(Path(FAILED_FILE), failed)
        log.warning(f"⚠️  {len(failed)} مهمة فاشلة حُفظت في '{FAILED_FILE}'")

    # إشارة إعادة التشغيل (للـ GitHub Actions)
    if needs_restart:
        Path(RESTART_SIGNAL_FILE).write_text("needs_restart", encoding="utf-8")
        log.info("⏰ الحد الزمني اقترب — تم رفع إشارة إعادة التشغيل")
    else:
        Path(RESTART_SIGNAL_FILE).unlink(missing_ok=True)

    print(f"\n{key_pool.status_report()}")
    print_summary(all_data)


# ─────────────────────────────────────────────────────────────────────
#  🔁  إعادة معالجة المهام الفاشلة
# ─────────────────────────────────────────────────────────────────────

def retry_failed():
    failed_path = Path(FAILED_FILE)
    if not failed_path.exists():
        print("لا يوجد ملف مهام فاشلة.")
        return

    failed_tasks = json.loads(failed_path.read_text(encoding="utf-8"))
    if not failed_tasks:
        print("قائمة المهام الفاشلة فارغة.")
        return

    log.info(f"🔄 إعادة معالجة {len(failed_tasks)} مهمة فاشلة...")

    key_pool   = KeyPool(GEMINI_API_KEYS, DELAY_PER_KEY)
    cp_manager = CheckpointManager(CHECKPOINT_FILE, OUTPUT_FILE)
    dataset, completed = cp_manager.load()

    stats = _Stats(total=len(failed_tasks))
    gen   = ParallelGenerator(key_pool, cp_manager, stats)

    all_data, still_failed, completed_new, _ = gen.run(
        tasks=failed_tasks, existing=dataset, completed=completed
    )

    merged = completed | completed_new
    cp_manager.save(all_data, merged)

    if still_failed:
        _atomic_write(failed_path, still_failed)
        log.warning(f"{len(still_failed)} مهمة لا تزال فاشلة.")
    else:
        failed_path.unlink(missing_ok=True)
        print("\n✅ تمت معالجة جميع المهام الفاشلة بفضل الله!")

    print_summary(all_data)


# ─────────────────────────────────────────────────────────────────────
#  🎬  نقطة الدخول
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]

    if "--retry-failed" in args:
        retry_failed()
    elif "--stats" in args:
        # عرض إحصاء ملف موجود فقط
        p = Path(OUTPUT_FILE)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            print_summary(data)
        else:
            print(f"الملف '{OUTPUT_FILE}' غير موجود.")
    else:
        run_generation()
