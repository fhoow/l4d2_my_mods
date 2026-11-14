# 說明

我的l4d2模組

- [armor_bod](/armor_bod/) 擊殺感染者會增加護甲
- [bod](/bod/) Dawn Shield skill
- [llm_director](/llm_director/) 使用llm控制


## llm_director說明
必備模組
left4dhooks

l4d2_llm_dire_standalonector版本是測試用包含策略的離線版 可以單獨運行

l4d2_llm_director_online版本是移到後端伺服器，使用left4dhooks作為與伺服器的橋樑，策略全部在伺服器生成與分析，使用RPC進行溝通
需要安裝[sm-ext-rpc](https://github.com/Dreae/sm-ext-rpc)
及運行python目前使用gemini_decision api進行測試 需要去AI-studio設定api key