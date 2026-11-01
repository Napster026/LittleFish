"""有限文本索引、查询意图与保守的近期归并；无模型调用。"""

import math
import re
import unicodedata


INDEX_VERSION = 2
QUERY_TERM_LIMIT = 16
RECALL_BATCH_SIZE = 128
_SEARCH_PARTICLES = str.maketrans("", "", "了的吗呢啊吧我你他她就才还又")

def normalized(text):
    return re.sub(r"[^\w\u4e00-\u9fff]", "", unicodedata.normalize("NFKC", text).casefold())


def search_terms(text):
    """仅对辅助检索去虚词；原文、归并键和否定词保持原样。"""
    text = unicodedata.normalize("NFKC", text).casefold().translate(_SEARCH_PARTICLES)
    terms = list(dict.fromkeys(re.findall(r"[a-z0-9_]{2,32}", text)))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        terms.extend((run,) if len(run)==1 else (run[i:i+2] for i in range(len(run)-1)))
    stop = {"我的", "什么", "怎么", "一下", "记得", "有没有", "可以", "的是", "了吗", "你还", "知道", "我不", "我要", "我会", "有没", "是不"}
    return tuple(w for w in dict.fromkeys(terms) if w not in stop)[:32]


def indexed_terms(record):
    topic=("出门","外出","出行") if record.claim_type=="plan" and record.topic_key=="plan:outing" else ()
    words=tuple(term for keyword in record.keywords for term in search_terms(keyword))
    return tuple(dict.fromkeys((*topic,*words,*search_terms(record.text))))[:36]


def lexical_similarity(query_terms, record_terms):
    """覆盖率与精度的调和平均（Dice）；重复和附加无关词不会加分。"""
    query,document=set(query_terms),set(record_terms)
    overlap=len(query & document)
    if not overlap:
        return 0.0
    coverage=overlap/len(query)
    precision=overlap/len(document)
    return 2*coverage*precision/(coverage+precision)


def recall_rank(record, query_terms, *, now, half_life_days=30.0, base_score=0.0):
    if not math.isfinite(half_life_days) or half_life_days<=0:
        raise ValueError("召回半衰期必须为有限正数")
    similarity=lexical_similarity(query_terms,indexed_terms(record))
    age_days=max(0.0,(now-record.updated_at).total_seconds())/86400
    decay=0.5 ** (age_days/half_life_days)
    return (base_score+2*similarity)*(0.5+0.5*record.importance)*decay


def _plan_display_key(record):
    if (record.claim_type!="plan" or record.visibility!="scope" or record.scope_id is None
            or record.about_id is None or not record.topic_key.strip() or not record.detail_key.strip()
            or record.target_start_at is None or record.target_end_at is None):
        return None
    return (record.agent_id,record.visibility,record.scope_id,record.kind,record.provenance,
            record.speaker_id,record.about_id,record.claim_type,record.topic_key,
            record.target_start_at,record.target_end_at,record.time_precision,record.plan_status)


def pick_for_display(ranked, limit):
    """消费已排序结果，在截断前去重；不修改记忆或把粗主题当作事件身份。"""
    if type(limit) is not int or limit<1:
        raise ValueError("展示条数必须为正整数")
    ranked=tuple(ranked)
    details={}
    for record,_ in ranked:
        key=_plan_display_key(record)
        if key is not None and record.detail_key!="outing":
            details.setdefault(key,set()).add(record.detail_key)
    picked=[]
    seen=set()
    for record,_ in ranked:
        key=_plan_display_key(record)
        if key is not None:
            # outing 是宽泛主题。只有一个具体安排时，粗细版本才可用同一展示组。
            single_outing=record.topic_key=="plan:outing" and len(details.get(key,()))==1
            identity=(key,None if single_outing else record.detail_key)
            if identity in seen:
                continue
            seen.add(identity)
        picked.append(record)
        if len(picked)==limit:
            break
    return tuple(picked)


def query_kind(text):
    if re.search(r"(?:今天|明天|后天|下周).*(?:干什么|做什么|做啥|干啥|去哪|去哪里|忙什么)",text):
        return "plan"
    if any(w in text for w in ("计划", "安排", "行程", "打算", "出行", "外出")):
        return "plan"
    if any(w in text for w in ("偏好", "喜好", "喜欢", "忌口", "口味", "不吃")):
        return "preference"
    if any(w in text for w in ("我是谁", "我的名字", "我叫什么")):
        return "identity"
    return None


def decide_update(draft, candidates, settings):
    """时间用于候选排序，不把最新当作必然正确；无法证明关系时新建。"""
    recent = []
    for old in candidates:
        age = (draft.occurred_at-old.updated_at).total_seconds()
        if not 0 <= age <= settings.merge_window_seconds:
            continue
        if old.valid_until is not None and old.valid_until<=draft.occurred_at:
            continue
        if old.valid_from is not None and old.valid_from>draft.occurred_at:
            continue
        if any(getattr(old,k) != getattr(draft,k) for k in
               ("kind","visibility","scope_id","about_id","speaker_id","claim_type","provenance")):
            continue
        recent.append(old)
    recent.sort(key=lambda m: (m.updated_at,m.memory_id), reverse=True)
    for old in recent:
        same_date = (old.target_start_at, old.target_end_at) == (draft.target_start_at, draft.target_end_at)
        same_topic = bool(draft.topic_key) and old.topic_key == draft.topic_key
        same_detail = bool(draft.detail_key) and old.detail_key == draft.detail_key
        if same_date and normalized(old.text) == normalized(draft.text):
            return old, "duplicate", old
        if not same_topic:
            continue
        if draft.claim_type == "plan":
            if draft.update_hint == "correct":
                peers=[m for m in recent if m.topic_key==draft.topic_key]
                if len(peers)==1:
                    return old, "corrected", draft
            if not same_date and not (draft.update_hint == "correct" and same_detail):
                continue
            if old.plan_status != "planned":
                continue  # 终态计划不能被普通完成/取消反馈覆盖；明确更正在上方处理。
            if draft.update_hint in ("cancel", "complete"):
                # 广义取消只在近期候选唯一时关联；具体取消必须对应完整同一细节。
                peers = [m for m in recent if m.topic_key==draft.topic_key and
                         (m.target_start_at,m.target_end_at)==(draft.target_start_at,draft.target_end_at)]
                if same_detail or (draft.detail_key=="outing" and len(peers)==1):
                    return old, "cancelled" if draft.update_hint=="cancel" else "completed", draft
                continue
            if same_detail and same_date:
                return old, "duplicate", old
            if draft.update_hint == "correct" and same_detail:
                return old, "corrected", draft
            if not same_date:
                continue
            peers=[m for m in recent if m.topic_key==draft.topic_key and
                   (m.target_start_at,m.target_end_at)==(draft.target_start_at,draft.target_end_at)]
            if old.detail_key == "outing" and draft.detail_key != "outing" and len(peers)==1:
                return old, "enriched", draft
            if draft.detail_key == "outing" and old.detail_key != "outing" and len(peers)==1:
                return old, "duplicate", old
            if (len(old.detail_key)>=2 and draft.detail_key.startswith(old.detail_key)
                    and len(draft.detail_key)>len(old.detail_key) and not re.search(r"不|没|取消|改", draft.detail_key)):
                return old, "enriched", draft
        elif same_detail:
            return old, "duplicate", old
        elif draft.update_hint == "correct":
            return old, "corrected", draft
    return None, "created", draft
