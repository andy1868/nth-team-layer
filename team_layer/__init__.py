"""
NTH DAO 鈥?Hermes Agent 鐨勫洟闃熷崗浣滃寮哄眰

鏋舵瀯锛?- runtime.py: TeamAgent (缁ф壙 Hermes Agent) + 璁板繂绠＄悊
- memory_providers/: 4 涓蹇?Provider锛圫OUL銆佺敤鎴锋ā鍨嬨€佸悜閲忋€佽处鏈級
- compression/: 5 灞備笂涓嬫枃鍘嬬缉绠＄嚎
- evolution/: EvoLoop 鑷繘鍖栧紩鎿?- git_sync/: 澶氱粓绔崗鍚屼笌鏃ュ織绠＄悊
- sandbox/: 闅旂鎵ц鐜

鍏抽敭璁捐鍘熷垯锛?1. 闆舵敼 Hermes 鍘熸枃浠?鈥?鎵€鏈夊姛鑳藉湪 team_layer/ 鍐呭疄鐜?2. 缁ф壙 + 閫傞厤鍣ㄦā寮?鈥?TeamAgent 缁ф壙 Hermes Agent锛屼笉鏀瑰師閫昏緫
3. Provider ABC 鈥?瀵规帴 Hermes 鐨?Memory Provider 鎺ュ彛
4. Git SSOT 鈥?鍒嗗竷寮忓崗鍚岀殑鍞竴鐪熷疄婧?"""

__version__ = "1.0.0"
__author__ = "NTH DAO Agent"

from .runtime import TeamAgent, TeamMemoryManager

__all__ = ["TeamAgent", "TeamMemoryManager"]
