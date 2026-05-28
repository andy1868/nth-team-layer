# Hermes Team Layer 榄旀敼瀹屾暣鎸囧崡

## 馃搶 蹇€熸瑙?
浣犲凡缁忔垚鍔熼瓟鏀?Hermes锛屽垱寤轰簡 **NTH DAO** 鈥?涓€涓畬鏁寸殑鍥㈤槦鍗忎綔 Agent 妗嗘灦銆傛牳蹇冪壒鐐癸細

| 鐗圭偣 | 璇存槑 |
|------|------|
| **闆舵敼 Hermes** | 鎵€鏈変唬鐮佸湪 `team_layer/` 闅旂锛孒ermes 鍘熸枃浠?100% 涓嶅姩 |
| **涓庝笂娓稿吋瀹?* | 鐢?`git rebase` 鍙案涔呬笌 Hermes 涓婃父鍚屾 |
| **鐢熶骇灏辩华** | PR 1-3 宸插疄瑁咃紙閫傞厤灞傘€佽蹇嗐€佸帇缂╋級锛屽彲鐩存帴浣跨敤 |
| **棰勭暀鎵╁睍** | PR 4-5 鐨勬帴鍙ｅ凡璁捐锛岀瓑寰呭疄鐜?|

## 馃幆 瀹炵幇鐨?3 涓?PR

### PR 1: Team Agent 閫傞厤鍣ㄥ眰 鉁?
**鏂囦欢**: `team_layer/runtime.py`

**鏍稿績绫?*:
- `MemoryProviderABC` 鈥?Memory Provider 鎶借薄鍩虹被
- `TeamMemoryManager` 鈥?缁熶竴璁板繂璋冨害
- `TeamAgent` 鈥?Hermes 鐨勫寮哄寘瑁咃紙缁ф壙锛屼笉鏀癸級

**鍏抽敭鐗规€?*:
- `get_system_prompt_with_memory()` 鈥?鎷兼帴甯﹁蹇嗙殑 system prompt
- `should_compact()` 鈥?妫€鏌ュ帇缂╂潯浠?- `trigger_compression()` 鈥?瑙﹀彂鍘嬬缉閽╁瓙
- `append_history()` 鈥?璁板綍浜や簰骞跺悓姝?Provider

**浣跨敤绀轰緥**:
```python
from team_layer import TeamAgent, TeamMemoryManager
from team_layer.memory_providers import SoulProvider

# 鍒涘缓璁板繂绠＄悊鍣?mem_mgr = TeamMemoryManager([SoulProvider()])

# 鍒涘缓 Team Agent
agent = TeamAgent("nlp-worker-1", team_memory_manager=mem_mgr)

# 鑾峰彇鍖呭惈鐏甸瓊鐨勭郴缁熸彁绀鸿瘝
prompt = agent.get_system_prompt_with_memory("base prompt")
```

---

### PR 2: 4 涓蹇?Provider 鉁?
**鐩綍**: `team_layer/memory_providers/`

#### SoulProvider锛堢伒榄傦級
- 浠?`skills/TEAM-SOUL.md` 鎳掑姞杞?- 浠呭姞杞?<200 token 鐨勬牳蹇冭鍒?- `on_pre_compress()` 淇濇姢鍏抽敭璇嶄笉琚憳鎺?
```python
soul = SoulProvider("skills/TEAM-SOUL.md")
soul.initialize({})
core = soul.prefetch("session_1")  # <200 token
```

#### UserModelProvider锛堢敤鎴锋ā鍨嬶級
- 瀛︿範鐢ㄦ埛鍋忓ソ锛圔ayesian 鏉冮噸鏇存柊锛?- 鍐崇瓥鍘嗗彶鑷姩淇濆瓨鍒?`memory/user-model.json`
- 璺ㄤ細璇濆涔?
```python
user = UserModelProvider()
user.record_decision({"type": "code_review"}, accepted=True)
user.on_session_end()  # 鑷姩淇濆瓨
```

#### VectorProvider锛堝悜閲忓簱锛?- 绱㈠紩 `skills/registry/` 涓嬬殑鎵€鏈夋妧鑳?- 绠€鍗曞叧閿瓧妫€绱紙鍚庣画鍗囩骇涓哄悜閲忔悳绱級
- `retrieve()` 杩斿洖鐩稿叧鎶€鑳?
```python
vector = VectorProvider("skills/registry")
vector.initialize({})
skills = vector.retrieve("鏁版嵁搴撹秴鏃?, top_k=3)
```

#### LedgerProvider锛堣处鏈級
- Append-only 鏃ュ織锛坄sidechain/ledger.jsonl`锛?- 璁板綍锛歵imestamp, agent_id, action_type, error_sig, token_cost
- 渚?EvoLoop 婧簮

```python
ledger = LedgerProvider()
ledger.record(
    agent_id="nlp-1",
    action_type="execute",
    result="success",
    error_sig=None,
    token_cost=100,
)
```

---

### PR 3: 5 灞傚帇缂╃绾?鉁?
**鏂囦欢**: `team_layer/compression/pipeline.py`

**5 涓帇缂╅樁娈?*锛堝粔浠蜂紭鍏堬級:

| 闃舵 | 鎴愭湰 | 瑙﹀彂 | 鍔ㄤ綔 |
|------|------|------|------|
| Budget Reduction | $0 | 50% | 闄嶄綆 effort_level |
| Snip History | $0 | 60% | 鎴柇 >5000 char 杈撳嚭 |
| Microcompact | $0.001 | 70% | 鍘嬬缉鏈€鍚?1-2 杞?|
| Context Collapse | $0.01 | 75% | 鍚堝苟 5 杞负鎽樿 |
| Auto-compact Summary | $0.05 | 85% | 璋冪敤 LLM + preserved-tail |

**浣跨敤绀轰緥**:
```python
from team_layer.compression import CompressionPipeline

pipeline = CompressionPipeline(history=agent.history)
msg = pipeline.auto_compress(threshold=0.75)
# 鑷姩閫夋嫨鍚堥€傜殑闃舵骞舵墽琛?```

---

## 馃殌 鍚姩涓庤繍琛?
### 閫夐」 1: 蹇€熷惎鍔紙宸查閰嶇疆锛?
```bash
cd hermes-team-agent

# Windows (PowerShell)
.\scripts\init_team.ps1

# Linux/Mac (Bash)
bash scripts/init_team.sh
```

### 閫夐」 2: 鎵嬪姩鍚姩

```bash
# 1. 鍒涘缓鍒嗘敮
git checkout -b team-layer-v1

# 2. 瀹夎渚濊禆
pip install -r requirements.txt
pip install -r requirements-team.txt

# 3. 杩愯 Team Agent
python team_entrypoint.py \
    --goal "閲嶆瀯璁よ瘉妯″潡" \
    --agent nlp-worker-1 \
    --iterations 5
```

**杈撳嚭绀轰緥**:
```
============================================================
Team Agent: nlp-worker-1
Goal: 閲嶆瀯璁よ瘉妯″潡
Session: nlp-worker-1_閲嶆瀯璁よ瘉妯″潡
============================================================

[SYSTEM PROMPT]
You are a helpful AI assistant...

<memory-context>
## TEAM SOUL
# TEAM SOUL (Core Summary)
...

--- Iteration 1 ---
Context usage: 5.0%
Progress: iteration 1
鉁?Completed 5 iterations
[INFO] Session nlp-worker-1_閲嶆瀯璁よ瘉妯″潡 finalized
```

---

## 馃搧 鏂囦欢澶圭害瀹?
### `team_layer/` 鈥?Team 涓撳睘浠ｇ爜锛堟柊澧烇級
```
team_layer/
鈹溾攢鈹€ __init__.py
鈹溾攢鈹€ runtime.py                  # PR 1 鏍稿績
鈹溾攢鈹€ memory_providers/           # PR 2 鏍稿績
鈹?  鈹溾攢鈹€ soul_provider.py
鈹?  鈹溾攢鈹€ user_model_provider.py
鈹?  鈹溾攢鈹€ vector_provider.py
鈹?  鈹斺攢鈹€ ledger_provider.py
鈹斺攢鈹€ compression/                # PR 3 鏍稿績
    鈹斺攢鈹€ pipeline.py
```

### `skills/` 鈥?鎶€鑳藉簱锛圙it 绠＄悊锛?```
skills/
鈹溾攢鈹€ TEAM-SOUL.md               # <200 token 鐏甸瓊鎽樿
鈹斺攢鈹€ registry/
    鈹溾攢鈹€ example_skill.md       # 绀轰緥鎶€鑳?    鈹斺攢鈹€ *.md                   # 鏇村鎶€鑳?```

### `memory/` 鈥?鎸佷箙鍖栬蹇嗭紙鏈湴锛?```
memory/
鈹溾攢鈹€ user-model.json            # 鐢ㄦ埛鍋忓ソ锛堣嚜鍔ㄧ敓鎴愶級
鈹斺攢鈹€ .gitignore                 # *.db 涓嶆彁浜?```

### `sidechain/` 鈥?Subagent 鍏ㄩ噺璁板綍
```
sidechain/
鈹斺攢鈹€ ledger.jsonl               # Append-only 璐︽湰
```

---

## 馃攧 涓?Hermes 涓婃父鍚屾

Team Layer 鐨勬渶澶т紭鍔匡細**姘歌繙鍙笌涓婃父鍚屾**锛屽洜涓烘病鏀瑰師鏂囦欢銆?
### 鏈堝害鍚屾娴佺▼

```bash
# 鑾峰彇涓婃父鏇存柊
git fetch upstream main

# 鍙樺熀鍒?team-layer-v1锛堣嚜鍔ㄥ鐞?Hermes 鏂囦欢鐨勬洿鏂帮級
git rebase upstream/main team-layer-v1

# 濡傛湁鍐茬獊锛屼粎鍦?team_layer/* 澶勭悊
# 锛圚ermes 鍘熸枃浠朵笉搴旀湁鍐茬獊锛?
# 楠岃瘉
git diff upstream/main hermes/  # 搴旇鏄剧ず 0 鏀瑰姩
git diff upstream/main team_layer/  # 鍙湅 Team 鏂板

# 鎺ㄩ€?git push origin team-layer-v1
```

---

## 馃К 鍚庣画鎵╁睍璺嚎锛堝凡棰勭暀鎺ュ彛锛?
### PR 4: EvoLoop 鑷繘鍖栧紩鎿?馃攧

**浣嶇疆**: `team_layer/evolution/`

**鍔熻兘**:
1. **Trigger** 鈥?浠?Ledger 缁熻閿欒
   - 鏉′欢锛歟rror_count 鈮?3 AND token_cost > budget * 1.5
2. **Reflector** 鈥?Subagent 鐢熸垚淇
   - 杈撳叆锛氬け璐ユ棩蹇?+ 閿欒绛惧悕
   - 杈撳嚭锛歅atch + Pydantic 濂戠害
3. **Verifier** 鈥?娌欑楠岃瘉
   - 鍦?Docker 涓繍琛?Patch
   - 鐢?Pydantic 鏍￠獙杈撳嚭
4. **Evolution Gate** 鈥?瀹℃壒
   - Low Risk锛圠int 淇锛夛細鑷姩 Merge
   - High Risk锛堟灦鏋勭骇锛夛細绛夊緟浜哄伐瀹℃壒

**棰勬湡浠ｇ爜缁撴瀯**:
```python
# team_layer/evolution/trigger.py
def should_evolve(error_sig: str, ledger: LedgerProvider) -> bool:
    count = ledger.count_error_occurrences(error_sig)
    cost = ledger.sum_token_cost_by_sig(error_sig)
    return count >= 3 and cost > EVOLUTION_BUDGET * 1.5

# team_layer/evolution/reflector.py
class ReflectorSubagent:
    def generate_patch(self, error_log: str) -> Patch:
        # 璋冪敤 LLM 鐢熸垚淇
        pass

# team_layer/evolution/verifier.py
class HybridVerifier:
    def verify_patch(self, patch: Patch) -> bool:
        # 鍦?Docker 娌欑杩愯 Patch
        pass
```

### PR 5: 澶氱粓绔崗鍚?馃攧

**浣嶇疆**: `team_layer/git_sync/`

**鍔熻兘**:
1. **Log Collector** 鈥?鏈湴鏃ュ織閲囬泦
   - 闆跺啿绐佸懡鍚嶏細`logs/{hostname}_{username}_{timestamp}.jsonl`
   - 鍚庡彴 cron锛氭瘡灏忔椂鑷姩 push
2. **Skill Loader** 鈥?鍘熷瓙绾х儹鍔犺浇
   - `git checkout origin/main -- skills/`
   - 鍙戜俊鍙疯 Agent 閲嶈浇
3. **Central Aggregator** 鈥?GitHub Action锛堟瘡鏃?23:00锛?   - 鑱氬悎鎵€鏈夋棩蹇?   - 鎵归噺鐢熸垚 Evolution PR
   - 绛夊緟浜哄伐瀹℃壒

**棰勬湡浠ｇ爜缁撴瀯**:
```python
# team_layer/git_sync/log_collector.py
class LogCollector:
    def collect(self, ledger: LedgerProvider) -> None:
        hostname = socket.gethostname()
        filename = f"logs/{hostname}_{self.user}_{int(time.time())}.jsonl"
        # 灏?ledger 鍐呭鍐欏叆鏂囦欢
        # git add/commit/push

# team_layer/git_sync/skill_loader.py
def atomic_reload_skills() -> bool:
    # git fetch origin main
    # git checkout origin/main -- skills/ TEAM-SOUL.md
    # 鍙戜俊鍙?pkill -HUP agent
    pass
```

**GitHub Action** (`.github/workflows/evolve_daily.yml`):
```yaml
name: Daily Evolution Review
on:
  schedule:
    - cron: "0 23 * * *"  # 姣忓ぉ 23:00

jobs:
  evolve:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Aggregate logs
        run: python scripts/aggregate_logs.py
      - name: Generate evolution PR
        run: python scripts/evo_cron.py
```

### PR 6: 鍔犲瘑浜ゆ槗 Agent 馃攧锛堝彲閫夛紝鍚庢湡锛?
**鍩轰簬**: TeamAgent 缁ф壙

**宸ュ叿**:
- `dex_swap.py` 鈥?DEX 浜ゆ崲锛圲niswap/1inch锛?- `price_oracle.py` 鈥?浠锋牸棰勮█鏈?- `wallet_signer.py` 鈥?閽卞寘绛惧悕
- `risk_monitor.py` 鈥?椋庨櫓棰勬祴

**瀹夊叏淇濊瘉**:
- 鎵€鏈変氦鏄撻€氳繃 7 灞傛潈闄?gating
- 閽卞寘绉侀挜涓嶈繘鍏?LLM context
- 浜ゆ槗缁撴灉鑷姩杩涘叆 EvoLoop锛堜紭鍖栫瓥鐣ワ級

```python
class CryptoTradingAgent(TeamAgent):
    def __init__(self, wallet_key: str, allowed_tokens: List[str], **kwargs):
        super().__init__(**kwargs)
        self.wallet = self._secure_init_wallet(wallet_key)
        self.register_tool("dex_swap", risk_level="high")

    def execute_trade(self, pair: str, amount: float):
        # 楂橀闄╋紝瑙﹀彂 Human Escalation锛? 灞傛潈闄愶級
        order = self.tools["dex_swap"].execute(pair, amount)
        self.evolution.record_trade_outcome(order)
        return order
```

---

## 馃搵 瀹炴柦娓呭崟

### 鐜板湪鍙仛锛圥R 1-3 瀹屾垚锛?- [ ] 浣跨敤 `team_entrypoint.py` 鍚姩 Team Agent
- [ ] 瀹氬埗 `TEAM-SOUL.md`锛堝鍔犲洟闃熺壒瀹氳鍒欙級
- [ ] 娣诲姞鎶€鑳藉埌 `skills/registry/`
- [ ] 鐩戞帶 `sidechain/ledger.jsonl` 鐨勯敊璇ā寮?- [ ] 涓?Hermes 涓婃父淇濇寔鍚屾锛坄git rebase`锛?
### 涓嬩竴姝ワ紙PR 4锛?- [ ] 瀹炵幇 EvoLoop锛圱rigger + Reflector + Verifier锛?- [ ] 闆嗘垚 Pydantic 濂戠害楠岃瘉
- [ ] 娴嬭瘯鑷姩淇娴佺▼

### 鍚庣画锛圥R 5锛?- [ ] 澶氱粓绔棩蹇楀崗鍚?- [ ] GitHub Action 姹囨€?+ PR 鐢熸垚
- [ ] 鍘熷瓙绾х儹鍔犺浇鑴氭湰

### 鍙€夛紙PR 6锛?- [ ] 鍔犲瘑浜ゆ槗 Agent
- [ ] Web3.py 闆嗘垚
- [ ] 椋庨櫓棰勬祴妯″瀷

---

## 馃洜锔?閰嶇疆涓庣幆澧冨彉閲?
### `.env.team`
```bash
AUTO_COMPACT_THRESHOLD=0.75       # 鍘嬬缉瑙﹀彂锛?0-85%锛?EVOLUTION_BUDGET=15000             # 杩涘寲鏈€澶?token 棰勭畻
EVO_REPO_PATH="."                  # Team 浠撳簱鏍硅矾寰?TEAM_MODE=true                     # 鍚敤 Team 妯″紡
```

### 鐜鍙橀噺璇存槑
- `AUTO_COMPACT_THRESHOLD`: 涓婁笅鏂囧崰鐢ㄧ巼杈惧埌姝ゅ€兼椂瑙﹀彂鍘嬬缉
- `EVOLUTION_BUDGET`: 鍗曟杩涘寲鍏佽鐨勬渶澶?token 鎴愭湰
- `EVO_REPO_PATH`: Team 浠撳簱鐨勬牴鐩綍锛堢敤浜?cron/action锛?- `TEAM_MODE`: 鏄惁鍚敤 Team Layer锛堝彲鐢ㄤ簬闄嶇骇鍒扮函 Hermes锛?
---

## 馃摎 鍏抽敭鏂囦欢瀵艰埅

| 鏂囦欢 | 璇存槑 |
|------|------|
| `team_entrypoint.py` | Team Agent 鍚姩鍏ュ彛 |
| `TEAM_LAYER_README.md` | 璇︾粏鎶€鏈枃妗?|
| `IMPLEMENTATION_GUIDE.md` | 鏈枃浠讹紙瀹炴柦鎸囧崡锛?|
| `requirements-team.txt` | Team 棰濆渚濊禆 |
| `scripts/init_team.ps1` | Windows 鍒濆鍖栬剼鏈?|
| `scripts/init_team.sh` | Linux/Mac 鍒濆鍖栬剼鏈?|

---

## 馃帗 璁捐鍝插鎬荤粨

### 涓轰粈涔堣繖鏍疯璁★紵

1. **闆舵敼涓婃父** 鈫?姘歌繙鍙?rebase 鍚屾 Hermes
2. **缁ф壙涓嶆敼** 鈫?TeamAgent 缁ф壙 HermesAgent锛屾棤姹℃煋
3. **鍒嗗眰璁板繂** 鈫?鐏甸瓊 + 鐢ㄦ埛 + 鍚戦噺 + 璐︽湰锛屽悇鍙稿叾鑱?4. **寤変环浼樺厛** 鈫?5 灞傚帇缂╋紝寤変环闃舵浼樺厛鎵ц
5. **Git SSOT** 鈫?鎵€鏈夌姸鎬侀€氳繃 append-only 鏃ュ織锛屽彲瀹¤銆佸彲鍥炴函
6. **寮傛杩涘寲** 鈫?EvoLoop 鍚庡彴杩愯锛屼笉闃绘柇涓诲惊鐜?
### 涓庡叾浠栨柟妗堢殑瀵规瘮

| 妗嗘灦 | 鍐崇瓥鏍稿績 | 鍩虹璁炬柦 | 鎸佷箙鍖?| 瀹夊叏 | 缂虹偣 |
|------|---------|---------|--------|------|------|
| Claude Code | 1.6% | 98.4% 鍐呯疆 | Session | 浼佷笟绾?| 涓婁笅鏂囨槗鐖?|
| OpenClaw | 20% | 80% 鑷 | 闀挎湡椹荤暀 | 鐢ㄦ埛鑷 | 缂哄皯鐜版垚淇濋殰 |
| CrewAI | 30% | 70% 澶栧寘 | 鏃犲唴缃?| 浠ｇ爜绾?| 鐏垫椿浣嗛渶鑷缓 |
| **NTH DAO** | **1.6%** | **98.4%** | **Git SSOT** | **7 灞?+ ML** | **瀹屾暣瑙ｅ喅鏂规** |

---

## 馃殌 蹇€熷懡浠ゅ弬鑰?
```bash
# 鍒濆鍖?./scripts/init_team.ps1                    # Windows
bash scripts/init_team.sh                  # Linux/Mac

# 杩愯
python team_entrypoint.py --goal "浠诲姟" --agent nlp-1 --iterations 10

# 鍚屾涓婃父
git fetch upstream main
git rebase upstream/main team-layer-v1

# 鎺ㄩ€佸埌鍥㈤槦浠撳簱
git remote add team-origin <your-private-repo>
git push team-origin team-layer-v1

# 鏌ョ湅鏃ュ織
tail -f sidechain/ledger.jsonl

# 鏌ョ湅鐢ㄦ埛妯″瀷
cat memory/user-model.json | python -m json.tool

# 鏌ョ湅鎶€鑳藉簱
ls -la skills/registry/
```

---

## 馃摓 甯歌闂

**Q: 濡備綍娣诲姞鑷畾涔?Provider锛?*
A: 缁ф壙 `MemoryProviderABC`锛屽疄鐜?5 涓挬瀛愶紝鍦?`team_entrypoint.py` 娉ㄥ唽銆?
**Q: 鍘嬬缉鍚庝細涓㈠け浠€涔堬紵**
A: preserved-tail 鏈哄埗淇濈暀鏈€杩?3 杞紝鍏抽敭淇℃伅鐢?Provider 鐨?`on_pre_compress()` 淇濇姢銆?
**Q: 濡備綍涓庡洟闃熷叡浜崌绾э紵**
A: 鎺ㄩ€佸埌绉佹湁 Git 浠撳簱锛屽洟闃熸垚鍛?pull + 鐑姞杞姐€?
**Q: 鏀寔瀹炴椂澶氫唬鐞嗗崗浣滃悧锛?*
A: 鏆傛椂涓嶆敮鎸侊紝PR 5锛堝缁堢鍗忓悓锛夊皢瀹炵幇姝ゅ姛鑳姐€?
---

## 馃摉 鍙傝€冧笌鑷磋阿

- **璁捐婧?*锛歂th Agent Pro 椤圭洰鐨?鍥㈤槦鍙繘鍖?AGENT"璁捐鏂囨。
- **鏋舵瀯鍙傝€?*锛欳laude Code銆丱penClaw銆丆rewAI 鐨勬渶浣冲疄璺?- **鍩虹妗嗘灦**锛欻ermes Agent (NousResearch)

---

**鐗堟湰**: Team Layer v1.0
**鐘舵€?*: PR 1-3 瀹屾垚锛孭R 4-5 棰勭暀
**鏈€鍚庢洿鏂?*: 2026-05-25
**缁存姢鑰?*: NTH DAO Agent
