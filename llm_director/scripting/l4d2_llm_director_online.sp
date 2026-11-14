/**
 * L4D2 AI Director — Online RPC Telemetry Exporter & Actuator (v0.5.0_online)
 *
 * Strictly delegates all tactical scoring, MVP/role/ammo aggregation, SI selection,
 * Anti-Hard-CC capping, Tank HP/count scaling, and Horde multipliers to Python via sm-ext-rpc.
 * SourcePawn retains only: raw telemetry collection, spatial/NavMesh spawn safety,
 * 28-slot crash protection, engine/VScript director bypasses, and offline RPC timeout fallback.
 */

#include <sourcemod>
#include <sdktools>
#include <sdkhooks>
#undef REQUIRE_PLUGIN
#include <left4dhooks>
#define REQUIRE_PLUGIN
#include <rpc>

#pragma semicolon 1
#pragma newdecls required

#define PLUGIN_VERSION          "0.5.0_online"

#define ZC_SMOKER               1
#define ZC_BOOMER               2
#define ZC_HUNTER               3
#define ZC_SPITTER              4
#define ZC_JOCKEY               5
#define ZC_CHARGER              6
#define ZC_WITCH                7
#define ZC_TANK                 8

#define TEAM_SURVIVOR           2
#define TEAM_INFECTED           3

#define MAX_SI_SLOTS            16
#define MAX_WAVE_SI             12
#define SAFE_CLIENT_SLOT_LIMIT  28
#define SPAWN_INTERVAL          0.35
#define SPAWN_DIST_MIN          300.0
#define SPAWN_DIST_MAX          1500.0
#define SPAWN_HEIGHT_THRESHOLD  110.0

#define NAV_SPAWN_CHECKPOINT    0x800
#define NAV_SPAWN_PLAYER_START  0x80
#define NAV_SPAWN_OBSCURED      0x1000

enum DirectorState
{
	STATE_DORMANT = 0,
	STATE_COOLDOWN,
	STATE_EVALUATE,
	STATE_SPAWNING,
	STATE_COMBAT
};

// Retained solely for local spatial/NavMesh spawn-tier geometry scoring
enum SpawnDirection
{
	DIR_FRONT = 0,
	DIR_MIDDLE_FLANK,
	DIR_BEHIND,
	DIR_ABOVE_ROOF,
	DIR_BELOW_FLOOR
};

static const char g_sStateNames[][] = { "DORMANT", "COOLDOWN", "EVALUATE", "SPAWNING", "COMBAT" };
static const char g_sDirNames[][]   = { "FRONT", "MIDDLE_FLANK", "BEHIND", "ABOVE_ROOF", "BELOW_FLOOR" };
static const char g_sSINames[][]    = { "None", "Smoker", "Boomer", "Hunter", "Spitter", "Jockey", "Charger", "Witch", "Tank" };
static const char g_sSICmdNames[][] = { "", "smoker", "boomer", "hunter", "spitter", "jockey", "charger", "witch", "tank" };
static const char g_sSIModels[][]   =
{
	"",
	"models/infected/smoker.mdl",
	"models/infected/boomer.mdl",
	"models/infected/hunter.mdl",
	"models/infected/spitter.mdl",
	"models/infected/jockey.mdl",
	"models/infected/charger.mdl",
	"",
	"models/infected/hulk.mdl"
};

char   g_sLogPath[PLATFORM_MAX_PATH];
Handle g_hCreateBot[9];
bool   g_bSDKCallsReady = false;

// Plugin ConVars
ConVar g_hCvarEnabled;
ConVar g_hCvarChatReport;
ConVar g_hCvarVerboseHookLog;
ConVar g_hCvarMinCooldown;
ConVar g_hCvarMaxCooldown;
ConVar g_hCvarMaxWaveSI;
ConVar g_hCvarHordeScale;
ConVar g_hCvarRpcServer;
ConVar g_hCvarRpcTimeout;
ConVar g_hCvarTickInterval;

// Managed Engine ConVars
ConVar g_hDirectorNoSpecials;
ConVar g_hDirectorAllowInfectedBots;
ConVar g_hZMaxPlayerZombies;
ConVar g_hZMinionLimit;
ConVar g_hZSpawnSafetyRange;
ConVar g_hZSmokerLimit;
ConVar g_hZBoomerLimit;
ConVar g_hZHunterLimit;
ConVar g_hZSpitterLimit;
ConVar g_hZJockeyLimit;
ConVar g_hZChargerLimit;

// Actuator & Telemetry State (decisions received from Python)
DirectorState  g_eState = STATE_DORMANT;
SpawnDirection g_eSpawnDir = DIR_FRONT;
char g_sLastStrategyUsed[64] = "offline_sm";
char g_sDiagName[48]         = "HEALTHY_STEADY_PUSH";
char g_sIntentName[48]       = "MAINTAIN_GAUNTLET_FLOW";
char g_sRoleName[32]         = "NONE";
char g_sHordeName[32]        = "KEEP_NATIVE";
char g_sBossName[32]         = "RESPECT_NATIVE";

bool  g_bLeftSafeArea         = false;
bool  g_bOurSpawn             = false;
bool  g_bSpawningExtraTank    = false;
bool  g_bFreezeAmbush         = false;
bool  g_bFocusStrike          = false;
bool  g_bSuppressCooldownMobs = false;
bool  g_bPanicEventActive     = false;
bool  g_bFinaleActive         = false;
bool  g_bWaitingForEvalReply  = false;
bool  g_bWaveWaitingForAmbush = false;

int   g_iCurrentEvalSeq        = 0;
int   g_iLastWaveIntent        = 5;
int   g_iWaveNumber            = 0;
int   g_iWaveTargetCount       = 0;
int   g_iWaveCursor            = 0;
int   g_iWaveSuccessCount      = 0;
int   g_iWaveQueue[MAX_WAVE_SI];
int   g_iTargetSurvivor        = 0;
int   g_iCurrentCooldown       = 20;
int   g_iWipeCount             = 0;
int   g_iNativeFinaleStage     = 18;
int   g_iPlannedTankHP         = 8000;
int   g_iPlannedExtraTanks     = 0;
int   g_iPlannedFocusSquadSize = 0;
float g_fPlannedHordeMult      = 0.8;

float g_fStateEnterTime        = 0.0;
float g_fCombatTimeout         = 28.0;
float g_fPeakFlowRatio         = 0.0;
float g_fPrevPeakFlowRatio     = 0.0;
float g_fLastFlowSampleTime    = 0.0;
float g_fFlowVelocity          = 0.0;
float g_fStagnationSeconds     = 0.0;
float g_fStagnationAnchor[3];
float g_fWaveCombatStartTime   = 0.0;
float g_fLastWaveClearDuration = 0.0;
float g_fLastAdditiveTankTime  = 0.0;
float g_fLastHeartbeatTime     = 0.0;
float g_fLastScriptValueLogTime = 0.0;
float g_fLastTeamFullLogTime   = 0.0;

bool  g_bIsFocusSquad[MAXPLAYERS + 1];
bool  g_bIsFrozenAmbush[MAXPLAYERS + 1];
float g_fFreezeExpireTime[MAXPLAYERS + 1];
float g_fLastChooseVictimLogTime[MAXPLAYERS + 1];
int   g_iPinningVictim[MAXPLAYERS + 1];

// Raw Per-Survivor Counters (exported directly to Python; never scored locally)
int   g_iKillsSI[MAXPLAYERS + 1];
int   g_iKillsCI[MAXPLAYERS + 1];
int   g_iHeadshots[MAXPLAYERS + 1];
int   g_iRevivesDone[MAXPLAYERS + 1];
int   g_iPinsCleared[MAXPLAYERS + 1];
int   g_iDamageDealtSI[MAXPLAYERS + 1];
int   g_iDamageTaken[MAXPLAYERS + 1];
int   g_iFriendlyFireDone[MAXPLAYERS + 1];
int   g_iIncapCount[MAXPLAYERS + 1];
int   g_iWaveDamageTaken = 0;
int   g_iWaveIncapCount  = 0;

Handle g_hMainTickTimer       = null;
Handle g_hSpawnQueueTimer     = null;
Handle g_hRpcEvalTimeoutTimer = null;

public Plugin myinfo =
{
	name        = "[L4D2] AI Director (Online RPC Edition)",
	author      = "Hua Server AI Director Team",
	description = "Lightweight raw telemetry exporter & game actuator for Python AI Director",
	version     = PLUGIN_VERSION,
	url         = ""
};

// ============================================================================
// Logging
// ============================================================================

void DirLog(const char[] format, any ...)
{
	if (g_sLogPath[0] == '\0')
	{
		BuildPath(Path_SM, g_sLogPath, sizeof(g_sLogPath), "logs/l4d2_llm_director_online.log");
	}
	char buffer[1024], mapName[64];
	VFormat(buffer, sizeof(buffer), format, 2);
	GetCurrentMap(mapName, sizeof(mapName));
	LogToFileEx(g_sLogPath, "[%s | %s | W#%d | t=%.1f] %s",
		mapName, g_sStateNames[g_eState], g_iWaveNumber, GetGameTime(), buffer);
}

void HookLog(const char[] hookName, const char[] format, any ...)
{
	if (g_sLogPath[0] == '\0')
	{
		BuildPath(Path_SM, g_sLogPath, sizeof(g_sLogPath), "logs/l4d2_llm_director_online.log");
	}
	char details[512];
	VFormat(details, sizeof(details), format, 3);
	LogToFileEx(g_sLogPath, "[HOOK:%s | %s | W#%d | t=%.1f] %s",
		hookName, g_sStateNames[g_eState], g_iWaveNumber, GetGameTime(), details);
}

// ============================================================================
// Lifecycle
// ============================================================================

public APLRes AskPluginLoad2(Handle myself, bool late, char[] error, int err_max)
{
	MarkNativeAsOptional("L4D2_RushVictim");
	MarkNativeAsOptional("L4D2_StartAssault");
	return APLRes_Success;
}

public void OnPluginStart()
{
	BuildPath(Path_SM, g_sLogPath, sizeof(g_sLogPath), "logs/l4d2_llm_director_online.log");
	DirLog("OnPluginStart: Loading L4D2 AI Director v%s (Online Thin Actuator)", PLUGIN_VERSION);

	InitSDKCalls();

	g_hCvarEnabled        = CreateConVar("l4d2_dir_online_enabled", "1", "Enable Online RPC L4D2 AI Director", FCVAR_NOTIFY, true, 0.0, true, 1.0);
	g_hCvarChatReport     = CreateConVar("l4d2_dir_online_chat_report", "1", "Show wave summary in chat", FCVAR_NOTIFY, true, 0.0, true, 1.0);
	g_hCvarVerboseHookLog = CreateConVar("l4d2_dir_online_verbose_hook_log", "1", "Log throttled engine hooks", FCVAR_NOTIFY, true, 0.0, true, 1.0);
	g_hCvarMinCooldown    = CreateConVar("l4d2_dir_online_min_cooldown", "10", "Minimum wave cooldown seconds", FCVAR_NOTIFY, true, 5.0, true, 60.0);
	g_hCvarMaxCooldown    = CreateConVar("l4d2_dir_online_max_cooldown", "35", "Maximum wave cooldown seconds", FCVAR_NOTIFY, true, 10.0, true, 90.0);
	g_hCvarMaxWaveSI      = CreateConVar("l4d2_dir_online_max_wave_si", "12", "Max Special Infected per wave", FCVAR_NOTIFY, true, 1.0, true, 12.0);
	g_hCvarHordeScale     = CreateConVar("l4d2_dir_online_horde_scale", "0.6", "User multiplier for common horde spawns", FCVAR_NOTIFY, true, 0.0, true, 2.0);
	g_hCvarRpcServer      = CreateConVar("l4d2_dir_online_rpc_server", "python", "Target RPC server name in configs/rpc.json", FCVAR_NOTIFY);
	g_hCvarRpcTimeout     = CreateConVar("l4d2_dir_online_rpc_timeout", "3.5", "Seconds before EvaluateWave falls back locally", FCVAR_NOTIFY, true, 1.0, true, 15.0);
	g_hCvarTickInterval   = CreateConVar("l4d2_dir_online_tick_interval", "2.0", "Heartbeat telemetry export interval (seconds)", FCVAR_NOTIFY, true, 1.0, true, 10.0);

	SetupEngineConVars();
	DisableOfflineDirectorIfPresent();

	RPCRegisterMethod("DirectorCommand", Rpc_DirectorCommand, Json);
	RPCRegisterMethod("GameChat", Rpc_GameChat, String);

	HookEvent("round_start",            Event_RoundStart,           EventHookMode_PostNoCopy);
	HookEvent("round_end",              Event_RoundEnd,             EventHookMode_PostNoCopy);
	HookEvent("mission_lost",           Event_MissionLost,          EventHookMode_PostNoCopy);
	HookEvent("map_transition",         Event_MapTransition,        EventHookMode_PostNoCopy);
	HookEvent("finale_vehicle_leaving", Event_FinaleVehicleLeaving, EventHookMode_PostNoCopy);
	HookEvent("finale_start",           Event_FinaleActivated,      EventHookMode_PostNoCopy);
	HookEvent("finale_radio_start",     Event_FinaleActivated,      EventHookMode_PostNoCopy);
	HookEvent("gauntlet_finale_start",  Event_GauntletFinaleStart,  EventHookMode_PostNoCopy);
	HookEvent("create_panic_event",     Event_CreatePanicEvent,     EventHookMode_PostNoCopy);
	HookEvent("player_left_safe_area",  Event_PlayerLeftSafeArea,   EventHookMode_PostNoCopy);
	HookEvent("door_open",              Event_DoorOpen,             EventHookMode_Post);

	HookEvent("player_hurt",            Event_PlayerHurt,           EventHookMode_Post);
	HookEvent("player_shoved",          Event_PlayerShoved,         EventHookMode_Post);
	HookEvent("player_incapacitated",   Event_PlayerIncap,          EventHookMode_Post);
	HookEvent("revive_success",         Event_ReviveSuccess,        EventHookMode_Post);
	HookEvent("player_death",           Event_PlayerDeath,          EventHookMode_Post);
	HookEvent("infected_death",         Event_InfectedDeath,        EventHookMode_Post);

	HookEvent("lunge_shove",            Event_PinStarted,           EventHookMode_Post);
	HookEvent("tongue_grab",            Event_PinStarted,           EventHookMode_Post);
	HookEvent("jockey_ride",            Event_PinStarted,           EventHookMode_Post);
	HookEvent("charger_pummel_start",   Event_PinStarted,           EventHookMode_Post);
	HookEvent("charger_carry_start",    Event_PinStarted,           EventHookMode_Post);
	HookEvent("pounce_stopped",         Event_PinCleared,           EventHookMode_Post);
	HookEvent("tongue_pull_stopped",    Event_PinCleared,           EventHookMode_Post);
	HookEvent("jockey_ride_end",        Event_PinCleared,           EventHookMode_Post);
	HookEvent("charger_pummel_end",     Event_PinCleared,           EventHookMode_Post);

	RegAdminCmd("sm_dironline_status",   Cmd_DirStatus,    ADMFLAG_GENERIC, "Print Online AI Director state");
	RegAdminCmd("sm_dironline_wave",     Cmd_DirWave,      ADMFLAG_CHEATS,  "Force EvaluateWave RPC request");
	RegAdminCmd("sm_dironline_spawnsi",  Cmd_DirSpawnSI,   ADMFLAG_CHEATS,  "Test spawning a specific SI class (1..6)");
	RegAdminCmd("sm_dironline_strategy", Cmd_SetStrategy,  ADMFLAG_CHEATS,  "Switch Python Director strategy");
	RegAdminCmd("sm_dironline_reload",   Cmd_ReloadPython, ADMFLAG_CHEATS,  "Hot-reload Python Director strategy modules");
	RegAdminCmd("sm_dironline_ping",     Cmd_PingPython,   ADMFLAG_GENERIC, "Ping Python AI Director RPC server");

	if (L4D_HasMapStarted() && IsAnySurvivorOutsideSafeRoom())
	{
		ActivateDirector(3.0, "LateLoad_OutsideSafeArea");
	}
}

public void OnAllPluginsLoaded()
{
	DisableOfflineDirectorIfPresent();
}

void DisableOfflineDirectorIfPresent()
{
	if (g_hCvarEnabled != null && !g_hCvarEnabled.BoolValue)
	{
		return;
	}
	ConVar hOffline = FindConVar("l4d2_dir_enabled");
	if (hOffline != null && hOffline.IntValue != 0)
	{
		hOffline.IntValue = 0;
		DirLog("CONFLICT_GUARD: Disabled offline l4d2_dir_enabled=0");
	}
}

public void OnPluginEnd()
{
	ResetDirectorState("OnPluginEnd");
	for (int i = 1; i <= MaxClients; i++)
	{
		if (IsClientInGame(i) && GetClientTeam(i) == TEAM_INFECTED)
		{
			SetEntityFlags(i, GetEntityFlags(i) & ~FL_FROZEN);
		}
	}
	RestoreEngineConVars();
}

public void OnMapStart()
{
	BuildPath(Path_SM, g_sLogPath, sizeof(g_sLogPath), "logs/l4d2_llm_director_online.log");
	for (int zc = ZC_SMOKER; zc <= ZC_TANK; zc++)
	{
		if (g_sSIModels[zc][0] != '\0')
		{
			PrecacheModel(g_sSIModels[zc], true);
		}
	}
	PrecacheModel("models/infected/common_male_riot.mdl", true);
	PrecacheModel("models/infected/common_male_ceda.mdl", true);
	PrecacheModel("models/infected/common_male_clown.mdl", true);

	g_iWipeCount = 0;
	DisableOfflineDirectorIfPresent();
	EnforceEngineConVars();
	ResetDirectorState("OnMapStart");

	char mapName[64];
	GetCurrentMap(mapName, sizeof(mapName));
	SendDirectorEventNotify("map_start", mapName);
}

public void OnMapEnd()
{
	char mapName[64];
	GetCurrentMap(mapName, sizeof(mapName));
	SendDirectorEventNotify("map_end", mapName);
	ResetDirectorState("OnMapEnd");
}

// ============================================================================
// Direct Bot Creation SDKCalls (`NextBotCreatePlayerBot<T>`)
// ============================================================================

void InitSDKCalls()
{
	static const int s_siClasses[7] = { ZC_SMOKER, ZC_BOOMER, ZC_HUNTER, ZC_SPITTER, ZC_JOCKEY, ZC_CHARGER, ZC_TANK };
	static const char s_sigNames[7][] =
	{
		"NextBotCreatePlayerBot<Smoker>", "NextBotCreatePlayerBot<Boomer>",
		"NextBotCreatePlayerBot<Hunter>", "NextBotCreatePlayerBot<Spitter>",
		"NextBotCreatePlayerBot<Jockey>", "NextBotCreatePlayerBot<Charger>",
		"NextBotCreatePlayerBot<Tank>"
	};

	GameData hGameConf = LoadGameConfigFile("all4dead2");
	if (hGameConf == null)
	{
		hGameConf = LoadGameConfigFile("l4dinfectedbots");
	}
	if (hGameConf == null)
	{
		DirLog("WARNING: all4dead2/l4dinfectedbots gamedata missing; NextBot SDKCalls disabled.");
		g_bSDKCallsReady = false;
		return;
	}

	Address jumpTable = GameConfGetAddress(hGameConf, "NextBotCreatePlayerBot.jumptable");
	if (jumpTable != Address_Null && LoadFromAddress(jumpTable, NumberType_Int8) == 0x68)
	{
		StringMap addrMap = new StringMap();
		for (int i = 0; i < 7; i++)
		{
			Address caseBase = jumpTable + view_as<Address>(i * 12);
			Address strAddr = view_as<Address>(LoadFromAddress(caseBase + view_as<Address>(1), NumberType_Int32));
			char siName[32];
			LoadStringFromAddress(strAddr, siName, sizeof(siName));
			int relOffset = LoadFromAddress(caseBase + view_as<Address>(6), NumberType_Int32);
			addrMap.SetValue(siName, caseBase + view_as<Address>(10) + view_as<Address>(relOffset));
		}
		for (int i = 0; i < 7; i++)
		{
			int zc = s_siClasses[i];
			Address fnAddr;
			if (addrMap.GetValue(g_sSINames[zc], fnAddr) && fnAddr != Address_Null)
			{
				StartPrepSDKCall(SDKCall_Static);
				if (PrepSDKCall_SetAddress(fnAddr))
				{
					PrepSDKCall_AddParameter(SDKType_String, SDKPass_Pointer);
					PrepSDKCall_SetReturnInfo(SDKType_CBasePlayer, SDKPass_Pointer);
					g_hCreateBot[zc] = EndPrepSDKCall();
				}
			}
		}
		delete addrMap;
	}
	else
	{
		for (int i = 0; i < 7; i++)
		{
			StartPrepSDKCall(SDKCall_Static);
			if (PrepSDKCall_SetFromConf(hGameConf, SDKConf_Signature, s_sigNames[i]))
			{
				PrepSDKCall_AddParameter(SDKType_String, SDKPass_Pointer);
				PrepSDKCall_SetReturnInfo(SDKType_CBasePlayer, SDKPass_Pointer);
				g_hCreateBot[s_siClasses[i]] = EndPrepSDKCall();
			}
		}
	}

	delete hGameConf;

	g_bSDKCallsReady = true;
	for (int i = 0; i < 7; i++)
	{
		if (g_hCreateBot[s_siClasses[i]] == null)
		{
			g_bSDKCallsReady = false;
			break;
		}
	}
	DirLog("InitSDKCalls: ready=%d", g_bSDKCallsReady ? 1 : 0);
}

void LoadStringFromAddress(Address addr, char[] buffer, int maxlength)
{
	int i = 0;
	while (i < maxlength - 1)
	{
		int val = LoadFromAddress(addr + view_as<Address>(i), NumberType_Int8);
		if (val == 0) break;
		buffer[i++] = view_as<char>(val);
	}
	buffer[i] = '\0';
}

int SpawnInfectedBotViaSDKCall(int zc, const float spawnPos[3], const float spawnAng[3])
{
	if (!g_bSDKCallsReady || zc < ZC_SMOKER || zc > ZC_TANK || g_hCreateBot[zc] == null || !HasSafeClientSlotMargin(1))
	{
		return 0;
	}

	g_bOurSpawn = true;
	int bot = SDKCall(g_hCreateBot[zc], g_sSINames[zc]);
	g_bOurSpawn = false;

	if (bot > 0 && bot <= MaxClients && IsClientInGame(bot))
	{
		SetEntityModel(bot, g_sSIModels[zc]);
		ChangeClientTeam(bot, TEAM_INFECTED);
		SetEntProp(bot, Prop_Send, "m_usSolidFlags", 16);
		SetEntProp(bot, Prop_Send, "movetype", 2);
		SetEntProp(bot, Prop_Send, "deadflag", 0);
		SetEntProp(bot, Prop_Send, "m_lifeState", 0);
		SetEntProp(bot, Prop_Send, "m_iObserverMode", 0);
		SetEntProp(bot, Prop_Send, "m_iPlayerState", 0);
		SetEntProp(bot, Prop_Send, "m_zombieState", 0);
		DispatchSpawn(bot);
		ActivateEntity(bot);
		TeleportEntity(bot, spawnPos, spawnAng, NULL_VECTOR);
		return bot;
	}
	return 0;
}

// ============================================================================
// Engine ConVar Unlocking
// ============================================================================

void SetupEngineConVars()
{
	g_hDirectorNoSpecials        = FindConVar("director_no_specials");
	g_hDirectorAllowInfectedBots = FindConVar("director_allow_infected_bots");
	g_hZMaxPlayerZombies         = FindConVar("z_max_player_zombies");
	g_hZMinionLimit              = FindConVar("z_minion_limit");
	g_hZSpawnSafetyRange         = FindConVar("z_spawn_safety_range");
	g_hZSmokerLimit              = FindConVar("z_smoker_limit");
	g_hZBoomerLimit              = FindConVar("z_boomer_limit");
	g_hZHunterLimit              = FindConVar("z_hunter_limit");
	g_hZSpitterLimit             = FindConVar("z_spitter_limit");
	g_hZJockeyLimit              = FindConVar("z_jockey_limit");
	g_hZChargerLimit             = FindConVar("z_charger_limit");

	if (g_hZMaxPlayerZombies != null)
	{
		SetConVarBounds(g_hZMaxPlayerZombies, ConVarBound_Upper, false);
		SetConVarBounds(g_hZMaxPlayerZombies, ConVarBound_Lower, false);
	}
	if (g_hZMinionLimit != null)
	{
		SetConVarBounds(g_hZMinionLimit, ConVarBound_Upper, false);
		SetConVarBounds(g_hZMinionLimit, ConVarBound_Lower, false);
	}
	EnforceEngineConVars();
}

void EnforceEngineConVars()
{
	if (g_hCvarEnabled != null && !g_hCvarEnabled.BoolValue)
	{
		return;
	}
	DisableOfflineDirectorIfPresent();

	if (g_hDirectorNoSpecials != null)        g_hDirectorNoSpecials.IntValue = 0;
	if (g_hDirectorAllowInfectedBots != null) g_hDirectorAllowInfectedBots.IntValue = 1;
	if (g_hZMaxPlayerZombies != null)         g_hZMaxPlayerZombies.IntValue = MAX_SI_SLOTS;
	if (g_hZMinionLimit != null)              g_hZMinionLimit.IntValue = MAX_SI_SLOTS;
	if (g_hZSpawnSafetyRange != null)         g_hZSpawnSafetyRange.IntValue = 250;
	if (g_hZSmokerLimit != null)              g_hZSmokerLimit.IntValue = 8;
	if (g_hZBoomerLimit != null)              g_hZBoomerLimit.IntValue = 8;
	if (g_hZHunterLimit != null)              g_hZHunterLimit.IntValue = 8;
	if (g_hZSpitterLimit != null)             g_hZSpitterLimit.IntValue = 8;
	if (g_hZJockeyLimit != null)              g_hZJockeyLimit.IntValue = 8;
	if (g_hZChargerLimit != null)             g_hZChargerLimit.IntValue = 8;
}

void RestoreEngineConVars()
{
	if (g_hDirectorNoSpecials != null)        g_hDirectorNoSpecials.RestoreDefault();
	if (g_hDirectorAllowInfectedBots != null) g_hDirectorAllowInfectedBots.RestoreDefault();
	if (g_hZMaxPlayerZombies != null)         g_hZMaxPlayerZombies.RestoreDefault();
	if (g_hZMinionLimit != null)              g_hZMinionLimit.RestoreDefault();
	if (g_hZSpawnSafetyRange != null)         g_hZSpawnSafetyRange.RestoreDefault();
	if (g_hZSmokerLimit != null)              g_hZSmokerLimit.RestoreDefault();
	if (g_hZBoomerLimit != null)              g_hZBoomerLimit.RestoreDefault();
	if (g_hZHunterLimit != null)              g_hZHunterLimit.RestoreDefault();
	if (g_hZSpitterLimit != null)             g_hZSpitterLimit.RestoreDefault();
	if (g_hZJockeyLimit != null)              g_hZJockeyLimit.RestoreDefault();
	if (g_hZChargerLimit != null)             g_hZChargerLimit.RestoreDefault();
}

// ============================================================================
// State Management
// ============================================================================

void ChangeState(DirectorState newState, const char[] reason)
{
	DirectorState oldState = g_eState;
	g_eState = newState;
	g_fStateEnterTime = GetGameTime();
	DirLog("STATE: %s -> %s (%s)", g_sStateNames[oldState], g_sStateNames[newState], reason);
}

void ResetDirectorState(const char[] reason)
{
	delete g_hMainTickTimer;
	delete g_hSpawnQueueTimer;
	delete g_hRpcEvalTimeoutTimer;

	g_bLeftSafeArea         = false;
	g_bOurSpawn             = false;
	g_bSpawningExtraTank    = false;
	g_bFreezeAmbush         = false;
	g_bFocusStrike          = false;
	g_bSuppressCooldownMobs = false;
	g_bPanicEventActive     = false;
	g_bFinaleActive         = false;
	g_bWaitingForEvalReply  = false;
	g_bWaveWaitingForAmbush = false;
	g_iCurrentEvalSeq++;

	g_eSpawnDir              = DIR_FRONT;
	g_iLastWaveIntent        = 5;
	g_iWaveNumber            = 0;
	g_iWaveTargetCount       = 0;
	g_iWaveCursor            = 0;
	g_iWaveSuccessCount      = 0;
	g_iTargetSurvivor        = 0;
	g_iCurrentCooldown       = 20;
	g_iNativeFinaleStage     = 18;
	g_iPlannedTankHP         = 8000;
	g_iPlannedExtraTanks     = 0;
	g_iPlannedFocusSquadSize = 0;
	g_fPlannedHordeMult      = 0.8;

	strcopy(g_sDiagName,   sizeof(g_sDiagName),   "HEALTHY_STEADY_PUSH");
	strcopy(g_sIntentName, sizeof(g_sIntentName), "MAINTAIN_GAUNTLET_FLOW");
	strcopy(g_sRoleName,   sizeof(g_sRoleName),   "NONE");
	strcopy(g_sHordeName,  sizeof(g_sHordeName),  "KEEP_NATIVE");
	strcopy(g_sBossName,   sizeof(g_sBossName),   "RESPECT_NATIVE");

	g_fPeakFlowRatio         = 0.0;
	g_fPrevPeakFlowRatio     = 0.0;
	g_fLastFlowSampleTime    = 0.0;
	g_fFlowVelocity          = 0.0;
	g_fStagnationSeconds     = 0.0;
	g_fStagnationAnchor[0]   = 0.0;
	g_fStagnationAnchor[1]   = 0.0;
	g_fStagnationAnchor[2]   = 0.0;
	g_fWaveCombatStartTime   = 0.0;
	g_fLastWaveClearDuration = 0.0;
	g_fLastAdditiveTankTime  = 0.0;
	g_fLastHeartbeatTime     = 0.0;
	g_iWaveDamageTaken       = 0;
	g_iWaveIncapCount        = 0;

	for (int i = 1; i <= MaxClients; i++)
	{
		g_bIsFocusSquad[i]            = false;
		g_bIsFrozenAmbush[i]          = false;
		g_fFreezeExpireTime[i]        = 0.0;
		g_fLastChooseVictimLogTime[i] = 0.0;
		g_iPinningVictim[i]           = 0;

		g_iKillsSI[i]                 = 0;
		g_iKillsCI[i]                 = 0;
		g_iHeadshots[i]               = 0;
		g_iRevivesDone[i]             = 0;
		g_iPinsCleared[i]             = 0;
		g_iDamageDealtSI[i]           = 0;
		g_iDamageTaken[i]             = 0;
		g_iFriendlyFireDone[i]        = 0;
		g_iIncapCount[i]              = 0;
	}

	ChangeState(STATE_DORMANT, reason);
}

void ActivateDirector(float initialCooldown, const char[] triggerSource)
{
	if (g_bLeftSafeArea || !g_hCvarEnabled.BoolValue)
	{
		return;
	}

	g_bLeftSafeArea = true;
	EnforceEngineConVars();

	g_fLastFlowSampleTime = GetGameTime();
	g_fLastHeartbeatTime  = 0.0;
	SampleTeamFlowAndStagnation();

	g_iCurrentCooldown = RoundToNearest(initialCooldown);
	ChangeState(STATE_COOLDOWN, triggerSource);

	delete g_hMainTickTimer;
	g_hMainTickTimer = CreateTimer(1.0, Timer_MainDirectorTick, _, TIMER_REPEAT);

	DirLog("ActivateDirector: %s | warmup=%ds", triggerSource, g_iCurrentCooldown);
	SendDirectorEventNotify("director_activated", triggerSource);

	if (g_hCvarChatReport.BoolValue)
	{
		PrintToChatAll("\x04[AI Director Online]\x01 Python RPC 導演已啟動 (\x03%s\x01) — 首波將於 \x05%ds\x01 後展開。",
			triggerSource, g_iCurrentCooldown);
	}
}

// ============================================================================
// Raw Telemetry Exporter & RPC Outbound Calls (SourceMod -> Python)
// Note: Avoid JSON.Push* due to sm-ext-rpc C++ stack index bugs; use JSON.Set*.
// ============================================================================

bool IsRpcTargetReady(char[] outServer, int maxlen)
{
	g_hCvarRpcServer.GetString(outServer, maxlen);
	return (outServer[0] != '\0' && RPCGetServerPort(outServer) > 0);
}

void GetSurvivorWeaponTelemetry(int client, char[] weaponName, int maxlen, int &clipAmmo, int &reserveAmmo)
{
	weaponName[0] = '\0';
	clipAmmo = 0;
	reserveAmmo = 0;

	int weapon = GetPlayerWeaponSlot(client, 0);
	if (weapon <= MaxClients || !IsValidEntity(weapon))
	{
		return;
	}

	GetEntityClassname(weapon, weaponName, maxlen);
	clipAmmo = HasEntProp(weapon, Prop_Send, "m_iClip1") ? GetEntProp(weapon, Prop_Send, "m_iClip1") : 0;
	if (clipAmmo < 0) clipAmmo = 0;

	if (GetFeatureStatus(FeatureType_Native, "L4D_GetReserveAmmo") == FeatureStatus_Available)
	{
		reserveAmmo = L4D_GetReserveAmmo(client, weapon);
	}
	else if (HasEntProp(weapon, Prop_Send, "m_iPrimaryAmmoType"))
	{
		int ammoType = GetEntProp(weapon, Prop_Send, "m_iPrimaryAmmoType");
		if (ammoType >= 0 && ammoType < 32)
		{
			reserveAmmo = GetEntProp(client, Prop_Send, "m_iAmmo", 4, ammoType);
		}
	}
	if (reserveAmmo < 0) reserveAmmo = 0;
}

JSON BuildTelemetryJSON(const char[] reason)
{
	g_iNativeFinaleStage = L4D2_GetCurrentFinaleStage();
	char mapName[64], keyBuf[16];
	GetCurrentMap(mapName, sizeof(mapName));

	JSON j = new JSON();
	j.SetInt("eval_seq", g_iCurrentEvalSeq);
	j.SetString("reason", reason);
	j.SetString("map_name", mapName);
	j.SetFloat("game_time", GetGameTime());
	j.SetInt("director_state", view_as<int>(g_eState));
	j.SetInt("wave_number", g_iWaveNumber);
	j.SetInt("last_wave_intent", g_iLastWaveIntent);

	j.SetInt("cur_finale_stage", g_iNativeFinaleStage);
	j.SetBool("finale_active", g_bFinaleActive);
	j.SetBool("panic_event_active", g_bPanicEventActive);
	j.SetBool("tank_alive", IsTankAliveInPlay());
	j.SetFloat("last_wave_clear_duration", g_fLastWaveClearDuration);
	j.SetInt("wipe_count", g_iWipeCount);
	j.SetInt("wave_damage_taken", g_iWaveDamageTaken);
	j.SetInt("wave_incap_count", g_iWaveIncapCount);
	j.SetFloat("peak_flow_ratio", g_fPeakFlowRatio);
	j.SetFloat("flow_velocity", g_fFlowVelocity);
	j.SetFloat("stagnation_seconds", g_fStagnationSeconds);

	j.SetInt("min_cooldown", g_hCvarMinCooldown.IntValue);
	j.SetInt("max_cooldown", g_hCvarMaxCooldown.IntValue);
	j.SetInt("max_wave_si", g_hCvarMaxWaveSI.IntValue);
	j.SetInt("client_count", GetClientCount(false));
	j.SetInt("active_si_total", CountActiveNonTankSI());

	int siCounts[7];
	for (int i = 1; i <= MaxClients; i++)
	{
		if (IsClientInGame(i) && GetClientTeam(i) == TEAM_INFECTED && IsPlayerAlive(i))
		{
			int zc = GetEntProp(i, Prop_Send, "m_zombieClass");
			if (zc >= ZC_SMOKER && zc <= ZC_CHARGER)
			{
				siCounts[zc]++;
			}
		}
	}

	JSON jSiCounts = new JSON();
	for (int zc = ZC_SMOKER; zc <= ZC_CHARGER; zc++)
	{
		IntToString(zc, keyBuf, sizeof(keyBuf));
		jSiCounts.SetInt(keyBuf, siCounts[zc]);
	}
	j.SetJSON("active_si_counts", jSiCounts);
	jSiCounts.Close();

	// Raw per-survivor observations only; Python computes all scores, rankings, and aggregations
	JSON jSurvivors = new JSON();
	int survCount = 0;
	ConVar hMaxIncap = FindConVar("survivor_max_incapacitated_count");
	int maxIncaps = (hMaxIncap != null) ? hMaxIncap.IntValue : 2;

	for (int i = 1; i <= MaxClients; i++)
	{
		if (!IsClientInGame(i) || GetClientTeam(i) != TEAM_SURVIVOR)
		{
			continue;
		}

		bool isAlive  = IsPlayerAlive(i);
		bool isIncap  = isAlive && IsSurvivorIncapOrHanging(i);
		bool isPinned = isAlive && IsSurvivorPinned(i);
		bool isBW     = isAlive && (
			(HasEntProp(i, Prop_Send, "m_bIsOnThirdStrike") && GetEntProp(i, Prop_Send, "m_bIsOnThirdStrike", 1) != 0) ||
			(HasEntProp(i, Prop_Send, "m_currentReviveCount") && GetEntProp(i, Prop_Send, "m_currentReviveCount") >= maxIncaps));

		int permHP = isAlive ? GetClientHealth(i) : 0;
		float tempHP = isAlive ? L4D_GetTempHealth(i) : 0.0;
		if (tempHP < 0.0) tempHP = 0.0;
		float flowDist = isAlive ? L4D2Direct_GetFlowDistance(i) : 0.0;

		char weaponName[64], sName[64], itemCls[64];
		int clipAmmo = 0, reserveAmmo = 0;
		bool hasMedkit = false, hasThrowable = false;
		float pos[3];

		if (isAlive)
		{
			GetSurvivorWeaponTelemetry(i, weaponName, sizeof(weaponName), clipAmmo, reserveAmmo);
			int slot2 = GetPlayerWeaponSlot(i, 2);
			hasThrowable = (slot2 > MaxClients && IsValidEntity(slot2));
			int slot3 = GetPlayerWeaponSlot(i, 3);
			if (slot3 > MaxClients && IsValidEntity(slot3))
			{
				GetEntityClassname(slot3, itemCls, sizeof(itemCls));
				hasMedkit = StrEqual(itemCls, "weapon_first_aid_kit", false);
			}
			GetClientAbsOrigin(i, pos);
		}

		GetClientName(i, sName, sizeof(sName));

		JSON jSurv = new JSON();
		jSurv.SetInt("client", i);
		jSurv.SetInt("userid", GetClientUserId(i));
		jSurv.SetString("name", sName);
		jSurv.SetBool("alive", isAlive);
		jSurv.SetBool("incap_or_hanging", isIncap);
		jSurv.SetBool("black_and_white", isBW);
		jSurv.SetBool("pinned", isPinned);
		jSurv.SetInt("perm_hp", permHP);
		jSurv.SetFloat("temp_hp", tempHP);
		jSurv.SetFloat("flow_dist", flowDist);
		jSurv.SetString("weapon_name", weaponName);
		jSurv.SetInt("clip_ammo", clipAmmo);
		jSurv.SetInt("reserve_ammo", reserveAmmo);
		jSurv.SetBool("has_medkit", hasMedkit);
		jSurv.SetBool("has_throwable", hasThrowable);
		jSurv.SetInt("kills_si", g_iKillsSI[i]);
		jSurv.SetInt("kills_ci", g_iKillsCI[i]);
		jSurv.SetInt("headshots", g_iHeadshots[i]);
		jSurv.SetInt("revives_done", g_iRevivesDone[i]);
		jSurv.SetInt("pins_cleared", g_iPinsCleared[i]);
		jSurv.SetInt("damage_dealt_si", g_iDamageDealtSI[i]);
		jSurv.SetInt("damage_taken", g_iDamageTaken[i]);
		jSurv.SetInt("friendly_fire_done", g_iFriendlyFireDone[i]);
		jSurv.SetInt("incap_count", g_iIncapCount[i]);
		jSurv.SetFloat("pos_x", pos[0]);
		jSurv.SetFloat("pos_y", pos[1]);
		jSurv.SetFloat("pos_z", pos[2]);

		IntToString(survCount, keyBuf, sizeof(keyBuf));
		jSurvivors.SetJSON(keyBuf, jSurv);
		jSurv.Close();
		survCount++;
	}

	jSurvivors.SetInt("count", survCount);
	j.SetJSON("survivors", jSurvivors);
	jSurvivors.Close();
	return j;
}

void SendDirectorEventNotify(const char[] eventName, const char[] details = "")
{
	char rpcServer[64];
	if (!IsRpcTargetReady(rpcServer, sizeof(rpcServer)))
	{
		return;
	}

	char mapName[64];
	GetCurrentMap(mapName, sizeof(mapName));

	JSON jEvt = new JSON();
	jEvt.SetString("event", eventName);
	jEvt.SetString("details", details);
	jEvt.SetString("map_name", mapName);
	jEvt.SetFloat("game_time", GetGameTime());
	jEvt.SetInt("director_state", view_as<int>(g_eState));
	jEvt.SetInt("wave_number", g_iWaveNumber);
	jEvt.SetInt("wipe_count", g_iWipeCount);
	jEvt.SetBool("finale_active", g_bFinaleActive);
	jEvt.SetBool("panic_event_active", g_bPanicEventActive);
	jEvt.SetFloat("last_wave_clear_duration", g_fLastWaveClearDuration);

	RPCCall call = new RPCCall();
	call.SetMethod("OnDirectorEvent");
	call.SetParams(jEvt);
	jEvt.Close();
	call.Notify(rpcServer);
}

void SendHeartbeatTickNotify()
{
	char rpcServer[64];
	if (!IsRpcTargetReady(rpcServer, sizeof(rpcServer)))
	{
		return;
	}

	JSON jTick = BuildTelemetryJSON("heartbeat");
	RPCCall call = new RPCCall();
	call.SetMethod("OnHeartbeatTick");
	call.SetParams(jTick);
	jTick.Close();
	call.Notify(rpcServer);
}

void RequestWaveEvaluationFromPython(const char[] reason, bool forceOverride = false)
{
	if (g_bWaitingForEvalReply && !forceOverride)
	{
		return;
	}

	char rpcServer[64];
	if (!IsRpcTargetReady(rpcServer, sizeof(rpcServer)))
	{
		ApplyBasicFallbackWave("RPC server unconfigured");
		return;
	}

	g_iCurrentEvalSeq++;
	g_bWaitingForEvalReply = true;

	delete g_hRpcEvalTimeoutTimer;
	g_hRpcEvalTimeoutTimer = CreateTimer(g_hCvarRpcTimeout.FloatValue, Timer_RpcEvalTimeout, g_iCurrentEvalSeq, TIMER_FLAG_NO_MAPCHANGE);

	JSON jTelemetry = BuildTelemetryJSON(reason);
	RPCCall call = new RPCCall();
	call.SetMethod("EvaluateWave");
	call.SetParams(jTelemetry);
	jTelemetry.Close();
	call.Send(rpcServer, OnRpcEvaluateWaveReply);

	DirLog("RPC_SEND [seq=%d]: EvaluateWave -> %s (%s)", g_iCurrentEvalSeq, rpcServer, reason);
}

public Action Timer_RpcEvalTimeout(Handle timer, int seq)
{
	g_hRpcEvalTimeoutTimer = null;
	if (!g_bWaitingForEvalReply || seq != g_iCurrentEvalSeq)
	{
		return Plugin_Stop;
	}

	g_bWaitingForEvalReply = false;
	DirLog("RPC_TIMEOUT: EvaluateWave seq=%d timed out (%.1fs) -> local basic fallback",
		seq, g_hCvarRpcTimeout.FloatValue);

	if (g_hCvarEnabled.BoolValue && g_bLeftSafeArea && g_eState == STATE_EVALUATE)
	{
		ApplyBasicFallbackWave("RPC timeout fallback");
	}
	return Plugin_Stop;
}

void ApplyBasicFallbackWave(const char[] reason)
{
	int anchor = ResolveAliveSurvivor(0);
	if (!IsValidAliveSurvivor(anchor))
	{
		g_iCurrentCooldown = 10;
		ChangeState(STATE_COOLDOWN, reason);
		return;
	}

	int queue[MAX_WAVE_SI];
	queue[0] = ZC_HUNTER;
	queue[1] = ZC_BOOMER;
	queue[2] = ZC_SMOKER;
	int count = ClampInt(3, 1, g_hCvarMaxWaveSI.IntValue);

	StartWaveSpawning(
		"local_fallback", reason,
		"FALLBACK", "BASIC_WAVE", "P1_FRONT", "KEEP_NATIVE", "RESPECT_NATIVE",
		g_iLastWaveIntent, anchor, DIR_FRONT,
		0.8, 8000, 0, 0, false,
		false, false, 20, count, queue,
		false, false, false, false, false, false);
}

public void OnRpcEvaluateWaveReply(JSON result)
{
	int evalSeq = result.GetInt("eval_seq");
	if (evalSeq != g_iCurrentEvalSeq)
	{
		result.Close();
		return;
	}

	g_bWaitingForEvalReply = false;
	delete g_hRpcEvalTimeoutTimer;

	if (!g_hCvarEnabled.BoolValue || !g_bLeftSafeArea)
	{
		result.Close();
		return;
	}

	bool skipWave = result.GetBool("skip_wave");
	int cooldown = result.GetInt("cooldown");
	char reason[128];
	result.GetString("reason", reason, sizeof(reason));

	if (skipWave)
	{
		result.Close();
		g_iCurrentCooldown = ClampInt(cooldown, 5, g_hCvarMaxCooldown.IntValue);
		DirLog("RPC_REPLY [seq=%d]: skip_wave=1 cd=%ds (%s)", evalSeq, g_iCurrentCooldown, reason);
		ChangeState(STATE_COOLDOWN, "Python skip_wave");
		return;
	}

	char strat[64], diagName[48], intentName[48], roleName[32], hordeName[32], bossName[32];
	result.GetString("strategy",            strat,      sizeof(strat));
	result.GetString("diagnosis_name",      diagName,   sizeof(diagName));
	result.GetString("intent_name",         intentName, sizeof(intentName));
	result.GetString("target_role_name",    roleName,   sizeof(roleName));
	result.GetString("horde_directive_name",hordeName,  sizeof(hordeName));
	result.GetString("boss_directive_name", bossName,   sizeof(bossName));

	int intent               = result.GetInt("intent");
	int targetSurvivor       = result.GetInt("target_survivor");
	SpawnDirection spawnDir  = view_as<SpawnDirection>(ClampInt(result.GetInt("spawn_dir"), 0, sizeof(g_sDirNames) - 1));
	float hordeScaleMult     = result.GetFloat("horde_scale_mult");
	int tankHP               = result.GetInt("tank_hp");
	int extraTanks           = result.GetInt("extra_tanks");
	int focusSquadSize       = result.GetInt("focus_squad_size");
	bool suppressCooldownMob = result.GetBool("suppress_cooldown_mobs");
	bool freezeAmbush        = result.GetBool("freeze_ambush");
	bool focusStrike         = result.GetBool("focus_strike");
	int waveCount            = result.GetInt("wave_target_count");
	bool clearPanicFlag      = result.GetBool("clear_panic_flag");
	bool decayScores         = result.GetBool("decay_scores");
	bool resetMobTimer       = result.GetBool("reset_mob_timer");
	bool triggerRushVictim   = result.GetBool("trigger_rush_victim");
	bool spawnRiotSquad      = result.GetBool("spawn_riot_squad");
	bool spawnMercyClown     = result.GetBool("spawn_mercy_clown");

	int queue[MAX_WAVE_SI];
	JSON jQueue = result.GetJSON("wave_queue");
	int qLen = jQueue.GetArraySize();
	if (qLen > MAX_WAVE_SI) qLen = MAX_WAVE_SI;
	for (int i = 0; i < qLen; i++)
	{
		queue[i] = jQueue.GetArrayInt(i);
	}
	jQueue.Close();
	result.Close();

	if (qLen < waveCount) waveCount = qLen;
	if (waveCount > g_hCvarMaxWaveSI.IntValue) waveCount = g_hCvarMaxWaveSI.IntValue;

	StartWaveSpawning(
		strat, reason,
		diagName, intentName, roleName, hordeName, bossName,
		intent, targetSurvivor, spawnDir,
		hordeScaleMult, tankHP, extraTanks, focusSquadSize, suppressCooldownMob,
		freezeAmbush, focusStrike, cooldown, waveCount, queue,
		clearPanicFlag, decayScores, resetMobTimer, triggerRushVictim, spawnRiotSquad, spawnMercyClown);
}

void FormatWaveComposition(char[] buffer, int maxlen)
{
	buffer[0] = '\0';
	for (int i = 0; i < g_iWaveTargetCount; i++)
	{
		int zc = g_iWaveQueue[i];
		if (i > 0) StrCat(buffer, maxlen, "+");
		StrCat(buffer, maxlen, (zc >= 1 && zc <= 8) ? g_sSINames[zc] : "?");
	}
}

void StartWaveSpawning(
	const char[] strategy, const char[] reason,
	const char[] diagName, const char[] intentName, const char[] roleName,
	const char[] hordeName, const char[] bossName,
	int intent, int targetSurvivor, SpawnDirection spawnDir,
	float hordeScaleMult, int tankHP, int extraTanks, int focusSquadSize, bool suppressCooldownMob,
	bool freezeAmbush, bool focusStrike, int cooldown, int waveCount, const int queue[MAX_WAVE_SI],
	bool clearPanicFlag, bool decayScores, bool resetMobTimer,
	bool triggerRushVictim, bool spawnRiotSquad, bool spawnMercyClown)
{
	strcopy(g_sLastStrategyUsed, sizeof(g_sLastStrategyUsed), strategy);
	strcopy(g_sDiagName,   sizeof(g_sDiagName),   diagName);
	strcopy(g_sIntentName, sizeof(g_sIntentName), intentName);
	strcopy(g_sRoleName,   sizeof(g_sRoleName),   roleName);
	strcopy(g_sHordeName,  sizeof(g_sHordeName),  hordeName);
	strcopy(g_sBossName,   sizeof(g_sBossName),   bossName);

	g_iLastWaveIntent        = intent;
	g_eSpawnDir              = spawnDir;
	g_fPlannedHordeMult      = (hordeScaleMult >= 0.0) ? hordeScaleMult : 0.8;
	g_iPlannedTankHP         = (tankHP > 0) ? tankHP : 8000;
	g_iPlannedExtraTanks     = ClampInt(extraTanks, 0, 4);
	g_iPlannedFocusSquadSize = ClampInt(focusSquadSize, 0, MAX_WAVE_SI);
	g_bSuppressCooldownMobs  = suppressCooldownMob;
	g_bFreezeAmbush          = freezeAmbush;
	g_bFocusStrike           = focusStrike;
	g_bWaveWaitingForAmbush  = false;
	g_iCurrentCooldown       = ClampInt(cooldown, g_hCvarMinCooldown.IntValue, g_hCvarMaxCooldown.IntValue);
	g_iTargetSurvivor        = ResolveAliveSurvivor(targetSurvivor);

	g_iWaveTargetCount = ClampInt(waveCount, 0, MAX_WAVE_SI);
	for (int i = 0; i < g_iWaveTargetCount; i++)
	{
		g_iWaveQueue[i] = ClampInt(queue[i], ZC_SMOKER, ZC_CHARGER);
	}

	if (clearPanicFlag)
	{
		g_bPanicEventActive = false;
	}
	g_iWaveDamageTaken = 0;
	g_iWaveIncapCount  = 0;

	if (decayScores)
	{
		for (int i = 1; i <= MaxClients; i++)
		{
			g_iKillsSI[i]       /= 2;
			g_iPinsCleared[i]   /= 2;
			g_iRevivesDone[i]   /= 2;
			g_iDamageDealtSI[i] /= 2;
		}
	}

	g_iWaveNumber++;
	g_iWaveCursor       = 0;
	g_iWaveSuccessCount = 0;

	char targetName[64] = "None", compSummary[192];
	if (IsValidAliveSurvivor(g_iTargetSurvivor))
	{
		GetClientName(g_iTargetSurvivor, targetName, sizeof(targetName));
	}
	FormatWaveComposition(compSummary, sizeof(compSummary));

	DirLog("WAVE_PLAN [W#%d | Strat=%s | %s]: Diag=%s Intent=%s Target=%s(%s) Dir=%s N=%d [%s] Horde=%.2fx Tank=%d(+%d) CD=%ds",
		g_iWaveNumber, strategy, reason, g_sDiagName, g_sIntentName,
		g_sRoleName, targetName, g_sDirNames[g_eSpawnDir], g_iWaveTargetCount, compSummary,
		g_fPlannedHordeMult, g_iPlannedTankHP, g_iPlannedExtraTanks, g_iCurrentCooldown);

	if (resetMobTimer) L4D_ResetMobTimer();
	if (triggerRushVictim && IsValidAliveSurvivor(g_iTargetSurvivor) &&
		GetFeatureStatus(FeatureType_Native, "L4D2_RushVictim") == FeatureStatus_Available)
	{
		L4D2_RushVictim(g_iTargetSurvivor, 1200.0);
	}
	ExecuteUncommonSideEffects(g_iTargetSurvivor, spawnRiotSquad, spawnMercyClown);

	if (g_hCvarChatReport.BoolValue)
	{
		PrintToChatAll("\x04[AI Director:%s]\x01 #%d | \x03%s\x01 -> \x03%s\x01 | 目標:\x05%s\x01 | \x03%s\x01 | %d隻 (%s)",
			strategy, g_iWaveNumber, g_sDiagName, g_sIntentName, targetName, g_sDirNames[g_eSpawnDir], g_iWaveTargetCount, compSummary);
	}

	g_fWaveCombatStartTime = GetGameTime();
	ChangeState(STATE_SPAWNING, "Starting wave spawn queue");
	delete g_hSpawnQueueTimer;
	g_hSpawnQueueTimer = CreateTimer(SPAWN_INTERVAL, Timer_SpawnQueueTick, _, TIMER_REPEAT);
}

void RequestFinaleBossEvaluationFromPython(int finaleType, const char[] arg)
{
	char rpcServer[64];
	if (!IsRpcTargetReady(rpcServer, sizeof(rpcServer)))
	{
		return;
	}

	JSON j = BuildTelemetryJSON("EvaluateFinaleBossStage");
	j.SetInt("finale_type", finaleType);
	j.SetString("finale_arg", arg);

	RPCCall call = new RPCCall();
	call.SetMethod("EvaluateFinaleBossStage");
	call.SetParams(j);
	j.Close();
	call.Send(rpcServer, OnRpcFinaleBossReply);
}

public void OnRpcFinaleBossReply(JSON result)
{
	char strat[64], reason[128];
	result.GetString("strategy",            strat,       sizeof(strat));
	result.GetString("reason",              reason,      sizeof(reason));
	result.GetString("diagnosis_name",      g_sDiagName, sizeof(g_sDiagName));
	result.GetString("boss_directive_name", g_sBossName, sizeof(g_sBossName));

	int tankHP = result.GetInt("tank_hp");
	int extraTanks = result.GetInt("extra_tanks");
	result.Close();

	if (tankHP > 0) g_iPlannedTankHP = tankHP;
	g_iPlannedExtraTanks = ClampInt(extraTanks, 0, 4);

	DirLog("FINALE_BOSS_STAGE [Strat=%s]: %s (TankHP=%d extra=%d, %s)",
		strat, g_sBossName, g_iPlannedTankHP, g_iPlannedExtraTanks, reason);
}

// ============================================================================
// Inbound RPC Methods (Python -> SourceMod)
// ============================================================================

public void Rpc_GameChat(RPCContext context)
{
	JSON params = context.GetParams();
	char msg[256];
	params.GetArrayString(0, msg, sizeof(msg));
	params.Close();

	DirLog("RPC_INBOUND [GameChat]: %s", msg);
	PrintToChatAll("\x04[AI Director]\x01 %s", msg);
	context.Done();
}

public void Rpc_DirectorCommand(RPCContext context)
{
	JSON params = context.GetParams();
	JSON cmd = params.GetArrayJSON(0);
	params.Close();

	char action[64], message[256];
	cmd.GetString("action", action, sizeof(action));
	int zc = cmd.GetInt("zombie_class");
	int targetSurvivor = cmd.GetInt("target_survivor");
	int spawnDir = cmd.GetInt("spawn_dir");
	bool focusSquad = cmd.GetBool("focus_squad");
	int cooldown = cmd.GetInt("cooldown");
	int hordeDir = cmd.GetInt("horde_directive");
	int bossDir = cmd.GetInt("boss_directive");
	cmd.GetString("message", message, sizeof(message));
	cmd.Close();

	DirLog("RPC_INBOUND [DirectorCommand]: action=%s zc=%d target=%d dir=%d msg=%s",
		action, zc, targetSurvivor, spawnDir, message);

	if (StrEqual(action, "force_wave", false))
	{
		if (!g_bLeftSafeArea) ActivateDirector(1.0, "RPC_force_wave");
		ChangeState(STATE_EVALUATE, "RPC DirectorCommand force_wave");
		RequestWaveEvaluationFromPython("RPC_force_wave", true);
	}
	else if (StrEqual(action, "spawn_si", false))
	{
		int anchor = ResolveAliveSurvivor(targetSurvivor);
		if (IsValidAliveSurvivor(anchor))
		{
			SpawnDirection sDir = view_as<SpawnDirection>(ClampInt(spawnDir, 0, sizeof(g_sDirNames) - 1));
			SpawnSpecialInfectedMultiTier(ClampInt(zc, ZC_SMOKER, ZC_CHARGER), anchor, sDir, focusSquad);
		}
	}
	else if (StrEqual(action, "spawn_tank", false))
	{
		if (message[0] != '\0')
		{
			int reqHP = StringToInt(message);
			if (reqHP > 0) g_iPlannedTankHP = reqHP;
		}
		bool spawnSet = (bossDir > 0);
		if (spawnSet)
		{
			g_iPlannedExtraTanks = ClampInt(bossDir, 0, 4);
		}

		int anchor = ResolveAliveSurvivor(targetSurvivor);
		if (IsValidAliveSurvivor(anchor) && HasSafeClientSlotMargin(1))
		{
			float pos[3], ang[3];
			if (!FindTier2AnySurvivorPZPos(anchor, ZC_TANK, pos))
			{
				GetClientAbsOrigin(anchor, pos);
				pos[2] += 20.0;
			}
			GetClientEyeAngles(anchor, ang);
			g_bSpawningExtraTank = !spawnSet;
			g_bOurSpawn = true;
			int tank = L4D2_SpawnTank(pos, ang);
			g_bOurSpawn = false;
			if (tank <= 0 || !IsClientInGame(tank))
			{
				tank = SpawnInfectedBotViaSDKCall(ZC_TANK, pos, ang);
			}
			g_bSpawningExtraTank = false;
			if (tank > 0 && IsClientInGame(tank))
			{
				CreateTimer(0.1, Timer_ApplyPlannedTankHP, GetClientUserId(tank), TIMER_FLAG_NO_MAPCHANGE);
			}
		}
	}
	else if (StrEqual(action, "spawn_uncommon", false))
	{
		int anchor = ResolveAliveSurvivor(targetSurvivor);
		if (IsValidAliveSurvivor(anchor) && message[0] != '\0')
		{
			ExecuteCheatCommand(anchor, "z_spawn_old", message);
		}
	}
	else if (StrEqual(action, "set_horde", false))
	{
		if (message[0] != '\0')
		{
			g_fPlannedHordeMult = FloatMax(0.0, StringToFloat(message));
		}
		else
		{
			static const float s_hordeMults[5] = { 0.8, 0.5, 1.0, 1.25, 1.0 };
			g_fPlannedHordeMult = s_hordeMults[ClampInt(hordeDir, 0, 4)];
		}
	}
	else if (StrEqual(action, "set_boss", false))
	{
		g_iPlannedExtraTanks = ClampInt(bossDir, 0, 2);
	}
	else if (StrEqual(action, "set_cooldown", false))
	{
		g_iCurrentCooldown = ClampInt(cooldown, g_hCvarMinCooldown.IntValue, g_hCvarMaxCooldown.IntValue);
	}
	else if (StrEqual(action, "reset_mob_timer", false))
	{
		L4D_ResetMobTimer();
	}
	else if (StrEqual(action, "rush_victim", false))
	{
		int anchor = ResolveAliveSurvivor(targetSurvivor);
		if (IsValidAliveSurvivor(anchor) && GetFeatureStatus(FeatureType_Native, "L4D2_RushVictim") == FeatureStatus_Available)
		{
			L4D2_RushVictim(anchor, 1200.0);
		}
	}
	else if (StrEqual(action, "start_assault", false))
	{
		if (GetFeatureStatus(FeatureType_Native, "L4D2_StartAssault") == FeatureStatus_Available)
		{
			L4D2_StartAssault();
		}
	}
	else if (StrEqual(action, "chat", false) && message[0] != '\0')
	{
		PrintToChatAll("\x04[AI Director]\x01 %s", message);
	}

	context.Done();
}

// ============================================================================
// Game Event Telemetry Collectors
// ============================================================================

public void Event_RoundStart(Event event, const char[] name, bool dontBroadcast)
{
	EnforceEngineConVars();
	ResetDirectorState("Event_RoundStart");
	SendDirectorEventNotify(name);
}

public void Event_RoundEnd(Event event, const char[] name, bool dontBroadcast)
{
	SendDirectorEventNotify(name);
	ResetDirectorState("Event_RoundEnd");
}

public void Event_MissionLost(Event event, const char[] name, bool dontBroadcast)
{
	g_iWipeCount++;
	SendDirectorEventNotify(name);
	ResetDirectorState("Event_MissionLost");
}

public void Event_MapTransition(Event event, const char[] name, bool dontBroadcast)
{
	SendDirectorEventNotify(name);
	ResetDirectorState("Event_MapTransition");
}

public void Event_FinaleVehicleLeaving(Event event, const char[] name, bool dontBroadcast)
{
	SendDirectorEventNotify(name);
	ResetDirectorState("Event_FinaleVehicleLeaving");
}

public void Event_FinaleActivated(Event event, const char[] name, bool dontBroadcast)
{
	g_bFinaleActive = true;
	SendDirectorEventNotify(name);
}

public void Event_GauntletFinaleStart(Event event, const char[] name, bool dontBroadcast)
{
	g_bFinaleActive = true;
	g_bPanicEventActive = true;
	SendDirectorEventNotify(name);
}

public void Event_CreatePanicEvent(Event event, const char[] name, bool dontBroadcast)
{
	g_bPanicEventActive = true;
	SendDirectorEventNotify(name);
}

public void Event_PlayerLeftSafeArea(Event event, const char[] name, bool dontBroadcast)
{
	if (!g_bLeftSafeArea)
	{
		ActivateDirector(10.0, "Event_PlayerLeftSafeArea");
	}
}

public void Event_DoorOpen(Event event, const char[] name, bool dontBroadcast)
{
	if (event.GetBool("checkpoint") && !g_bLeftSafeArea)
	{
		int client = GetClientOfUserId(event.GetInt("userid"));
		if (client > 0 && IsClientInGame(client) && GetClientTeam(client) == TEAM_SURVIVOR)
		{
			CreateTimer(2.5, Timer_CheckSafeRoomExit, _, TIMER_FLAG_NO_MAPCHANGE);
		}
	}
}

public Action Timer_CheckSafeRoomExit(Handle timer)
{
	if (!g_bLeftSafeArea && IsAnySurvivorOutsideSafeRoom())
	{
		ActivateDirector(8.0, "DoorOpen_OutsideSafeRoom");
	}
	return Plugin_Stop;
}

public void Event_PlayerHurt(Event event, const char[] name, bool dontBroadcast)
{
	int victim   = GetClientOfUserId(event.GetInt("userid"));
	int attacker = GetClientOfUserId(event.GetInt("attacker"));
	int dmg      = event.GetInt("dmg_health");
	if (victim <= 0 || victim > MaxClients || !IsClientInGame(victim))
	{
		return;
	}

	int victimTeam = GetClientTeam(victim);
	if (victimTeam == TEAM_SURVIVOR && dmg > 0)
	{
		g_iDamageTaken[victim] += dmg;
		if (!IsSurvivorIncapOrHanging(victim)) g_iWaveDamageTaken += dmg;
		if (attacker > 0 && attacker <= MaxClients && IsClientInGame(attacker) &&
			GetClientTeam(attacker) == TEAM_SURVIVOR && attacker != victim)
		{
			g_iFriendlyFireDone[attacker] += dmg;
		}
	}
	else if (victimTeam == TEAM_INFECTED)
	{
		if (dmg > 0 && attacker > 0 && attacker <= MaxClients && IsClientInGame(attacker) && GetClientTeam(attacker) == TEAM_SURVIVOR)
		{
			g_iDamageDealtSI[attacker] += dmg;
		}
		if (g_bIsFrozenAmbush[victim])
		{
			UnfreezeSI(victim, "DamagedBySurvivor");
		}
	}
}

public void Event_PlayerShoved(Event event, const char[] name, bool dontBroadcast)
{
	int victim   = GetClientOfUserId(event.GetInt("userid"));
	int attacker = GetClientOfUserId(event.GetInt("attacker"));
	if (victim > 0 && victim <= MaxClients && IsClientInGame(victim) && GetClientTeam(victim) == TEAM_INFECTED &&
		g_iPinningVictim[victim] > 0 && attacker > 0 && attacker <= MaxClients &&
		IsClientInGame(attacker) && GetClientTeam(attacker) == TEAM_SURVIVOR)
	{
		g_iPinsCleared[attacker]++;
		g_iPinningVictim[victim] = 0;
	}
}

public void Event_PlayerIncap(Event event, const char[] name, bool dontBroadcast)
{
	int client = GetClientOfUserId(event.GetInt("userid"));
	if (client > 0 && client <= MaxClients && IsClientInGame(client) && GetClientTeam(client) == TEAM_SURVIVOR)
	{
		g_iIncapCount[client]++;
		g_iWaveIncapCount++;
		char details[64];
		FormatEx(details, sizeof(details), "victim=%N incaps=%d", client, g_iIncapCount[client]);
		SendDirectorEventNotify(name, details);
	}
}

public void Event_ReviveSuccess(Event event, const char[] name, bool dontBroadcast)
{
	int subject = GetClientOfUserId(event.GetInt("subject"));
	int helper  = GetClientOfUserId(event.GetInt("userid"));
	if (helper > 0 && helper <= MaxClients && IsClientInGame(helper) && GetClientTeam(helper) == TEAM_SURVIVOR && helper != subject)
	{
		g_iRevivesDone[helper]++;
	}
}

public void Event_PlayerDeath(Event event, const char[] name, bool dontBroadcast)
{
	int victim   = GetClientOfUserId(event.GetInt("userid"));
	int attacker = GetClientOfUserId(event.GetInt("attacker"));
	bool headshot = event.GetBool("headshot");
	if (victim <= 0 || victim > MaxClients || !IsClientInGame(victim))
	{
		return;
	}

	int victimTeam = GetClientTeam(victim);
	if (victimTeam == TEAM_INFECTED)
	{
		int zc = GetEntProp(victim, Prop_Send, "m_zombieClass");
		g_bIsFocusSquad[victim]   = false;
		g_bIsFrozenAmbush[victim] = false;
		SetEntityFlags(victim, GetEntityFlags(victim) & ~FL_FROZEN);

		if (attacker > 0 && attacker <= MaxClients && IsClientInGame(attacker) && GetClientTeam(attacker) == TEAM_SURVIVOR)
		{
			g_iKillsSI[attacker]++;
			if (headshot) g_iHeadshots[attacker]++;
			if (g_iPinningVictim[victim] > 0 && attacker != g_iPinningVictim[victim])
			{
				g_iPinsCleared[attacker]++;
			}
		}
		g_iPinningVictim[victim] = 0;

		if (CountActiveNonTankSI() == 0 && g_eState == STATE_COMBAT && g_fWaveCombatStartTime > 0.0)
		{
			g_fLastWaveClearDuration = FloatMax(0.1, GetGameTime() - g_fWaveCombatStartTime);
			g_fWaveCombatStartTime = 0.0;
			char details[64];
			FormatEx(details, sizeof(details), "wave=%d clear_sec=%.1f", g_iWaveNumber, g_fLastWaveClearDuration);
			SendDirectorEventNotify("wave_cleared", details);
		}
		if (zc == ZC_TANK)
		{
			SendDirectorEventNotify("tank_killed");
		}
		if (IsFakeClient(victim) && zc != ZC_TANK)
		{
			CreateTimer((zc == ZC_SPITTER) ? 6.0 : 0.5, Timer_KickDeadSIBot, GetClientUserId(victim), TIMER_FLAG_NO_MAPCHANGE);
		}
	}
	else if (victimTeam == TEAM_SURVIVOR)
	{
		g_iWaveIncapCount++;
		char details[64];
		FormatEx(details, sizeof(details), "survivor=%N", victim);
		SendDirectorEventNotify("survivor_death", details);
	}
}

public Action Timer_KickDeadSIBot(Handle timer, int userid)
{
	int client = GetClientOfUserId(userid);
	if (client > 0 && client <= MaxClients && IsClientInGame(client) && IsFakeClient(client) &&
		!IsClientInKickQueue(client) && GetClientTeam(client) == TEAM_INFECTED && !IsPlayerAlive(client))
	{
		KickClient(client, "SI dead slot recycle");
	}
	return Plugin_Stop;
}

public void Event_InfectedDeath(Event event, const char[] name, bool dontBroadcast)
{
	int attacker = GetClientOfUserId(event.GetInt("attacker"));
	if (attacker > 0 && attacker <= MaxClients && IsClientInGame(attacker) && GetClientTeam(attacker) == TEAM_SURVIVOR)
	{
		g_iKillsCI[attacker]++;
		if (event.GetBool("headshot")) g_iHeadshots[attacker]++;
	}
}

public void Event_PinStarted(Event event, const char[] name, bool dontBroadcast)
{
	int attacker = GetClientOfUserId(event.GetInt("userid"));
	int victim   = GetClientOfUserId(event.GetInt("victim"));
	if (attacker > 0 && attacker <= MaxClients && victim > 0 && victim <= MaxClients)
	{
		g_iPinningVictim[attacker] = victim;
	}
}

public void Event_PinCleared(Event event, const char[] name, bool dontBroadcast)
{
	int savior = GetClientOfUserId(event.GetInt("userid"));
	int victim = GetClientOfUserId(event.GetInt("victim"));
	if (savior > 0 && savior <= MaxClients && IsClientInGame(savior) && GetClientTeam(savior) == TEAM_SURVIVOR && savior != victim)
	{
		g_iPinsCleared[savior]++;
	}
	for (int i = 1; i <= MaxClients && victim > 0; i++)
	{
		if (g_iPinningVictim[i] == victim) g_iPinningVictim[i] = 0;
	}
}

// ============================================================================
// Left4DHooks Engine Overrides & Boss/Horde Actuators
// ============================================================================

public Action L4D_OnIsTeamFull(int team, bool &full)
{
	if (!g_hCvarEnabled.BoolValue || team != TEAM_INFECTED || !full || !HasSafeClientSlotMargin(1))
	{
		return Plugin_Continue;
	}

	full = false;
	float now = GetGameTime();
	if (g_hCvarVerboseHookLog.BoolValue && now - g_fLastTeamFullLogTime >= 10.0)
	{
		g_fLastTeamFullLogTime = now;
		HookLog("L4D_OnIsTeamFull", "Overrode infected team full -> false");
	}
	return Plugin_Changed;
}

bool TryOverrideDirectorScriptInt(const char[] key, int &retVal)
{
	if (StrEqual(key, "ShouldAllowSpecialsWithTank", false) || StrEqual(key, "ShouldAllowMobsWithTank", false))
	{
		retVal = 1;
		return true;
	}
	if (StrEqual(key, "MaxSpecials", false) || StrEqual(key, "cm_MaxSpecials", false) ||
		StrEqual(key, "cm_BaseSpecialLimit", false) || StrEqual(key, "DominatorLimit", false) ||
		StrEqual(key, "cm_DominatorLimit", false) || StrContains(key, "Limit", false) != -1 &&
		(StrEqual(key, "SmokerLimit", false) || StrEqual(key, "BoomerLimit", false) ||
		StrEqual(key, "HunterLimit", false) || StrEqual(key, "SpitterLimit", false) ||
		StrEqual(key, "JockeyLimit", false) || StrEqual(key, "ChargerLimit", false) ||
		StrEqual(key, "cm_SmokerLimit", false) || StrEqual(key, "cm_BoomerLimit", false) ||
		StrEqual(key, "cm_HunterLimit", false) || StrEqual(key, "cm_SpitterLimit", false) ||
		StrEqual(key, "cm_JockeyLimit", false) || StrEqual(key, "cm_ChargerLimit", false)))
	{
		retVal = MAX_SI_SLOTS;
		return true;
	}
	return false;
}

Action HandleScriptValueInt(const char[] hookName, const char[] key, int &retVal)
{
	if (!g_hCvarEnabled.BoolValue || !TryOverrideDirectorScriptInt(key, retVal))
	{
		return Plugin_Continue;
	}
	float now = GetGameTime();
	if (g_hCvarVerboseHookLog.BoolValue && now - g_fLastScriptValueLogTime >= 10.0)
	{
		g_fLastScriptValueLogTime = now;
		HookLog(hookName, "key=%s -> %d", key, retVal);
	}
	return Plugin_Handled;
}

public Action L4D_OnGetScriptValueInt(const char[] key, int &retVal)              { return HandleScriptValueInt("L4D_OnGetScriptValueInt", key, retVal); }
public Action L4D2_OnGetScriptValueInt(const char[] key, int &retVal, int scope)  { return HandleScriptValueInt("L4D2_OnGetScriptValueInt", key, retVal); }

Action HandleScriptValueFloat(const char[] key, float &retVal)
{
	if (g_hCvarEnabled.BoolValue && (StrEqual(key, "SpecialRespawnInterval", false) || StrEqual(key, "cm_SpecialRespawnInterval", false)))
	{
		retVal = 999999.0;
		return Plugin_Handled;
	}
	return Plugin_Continue;
}

public Action L4D_OnGetScriptValueFloat(const char[] key, float &retVal)             { return HandleScriptValueFloat(key, retVal); }
public Action L4D2_OnGetScriptValueFloat(const char[] key, float &retVal, int scope) { return HandleScriptValueFloat(key, retVal); }

public Action L4D_OnFirstSurvivorLeftSafeArea(int client)
{
	if (!g_bLeftSafeArea)
	{
		ActivateDirector(10.0, "L4D_OnFirstSurvivorLeftSafeArea");
	}
	return Plugin_Continue;
}

public Action L4D2_OnChangeFinaleStage(int &finaleType, const char[] arg)
{
	g_iNativeFinaleStage = finaleType;
	if (finaleType == FINALE_NONE)
	{
		g_bFinaleActive = false;
		strcopy(g_sBossName, sizeof(g_sBossName), "RESPECT_NATIVE");
		g_iPlannedExtraTanks = 0;
		return Plugin_Continue;
	}

	g_bFinaleActive = true;

	bool isBossStage = (finaleType == FINALE_HALFTIME_BOSS || finaleType == FINALE_FINAL_BOSS ||
		finaleType == FINALE_CUSTOM_TANK || finaleType == FINALE_GAUNTLET_BOSS_INCOMING || finaleType == FINALE_GAUNTLET_BOSS);

	if (isBossStage)
	{
		RequestFinaleBossEvaluationFromPython(finaleType, arg);
	}
	else
	{
		strcopy(g_sBossName, sizeof(g_sBossName), "RESPECT_NATIVE");
		g_iPlannedExtraTanks = 0;
	}

	char details[96];
	FormatEx(details, sizeof(details), "stage=%d boss=%d arg=%s", finaleType, isBossStage ? 1 : 0, arg);
	SendDirectorEventNotify("finale_stage_changed", details);
	return Plugin_Continue;
}

public Action L4D_OnSpawnSpecial(int &zombieClass, const float vecPos[3], const float vecAng[3])
{
	if (!g_hCvarEnabled.BoolValue || !g_bLeftSafeArea)
	{
		return Plugin_Continue;
	}
	return g_bOurSpawn ? Plugin_Continue : Plugin_Handled;
}

Action HandleMobSpawn(int &amount, const char[] hookName)
{
	if (!g_hCvarEnabled.BoolValue || !g_bLeftSafeArea)
	{
		return Plugin_Continue;
	}
	int orig = amount;
	float mult = (g_fPlannedHordeMult >= 0.0) ? g_fPlannedHordeMult : 0.8;
	float userScale = g_hCvarHordeScale.FloatValue;
	amount = (userScale <= 0.0 || mult <= 0.0) ? 0 : RoundToNearest(float(amount) * mult * userScale);
	if (amount < 0) amount = 0;
	if (g_hCvarVerboseHookLog.BoolValue)
	{
		HookLog(hookName, "%s mult=%.2f orig=%d -> %d", g_sHordeName, mult, orig, amount);
	}
	return (amount != orig) ? Plugin_Changed : Plugin_Continue;
}

public Action L4D_OnSpawnMob(int &amount)   { return HandleMobSpawn(amount, "L4D_OnSpawnMob"); }
public Action L4D_OnSpawnITMob(int &amount) { return HandleMobSpawn(amount, "L4D_OnSpawnITMob"); }

public void L4D_OnSpawnTank_Post(int client, const float vecPos[3], const float vecAng[3])
{
	if (!g_hCvarEnabled.BoolValue)
	{
		return;
	}
	SendDirectorEventNotify("tank_spawned");
	// Delay 0.12s so async EvaluateFinaleBossStage RPC replies update planned Tank HP/count
	DataPack pack = new DataPack();
	pack.WriteCell((client > 0 && client <= MaxClients && IsClientInGame(client)) ? GetClientUserId(client) : 0);
	pack.WriteFloat(vecPos[0]);
	pack.WriteFloat(vecPos[1]);
	pack.WriteFloat(vecPos[2]);
	pack.WriteFloat(vecAng[0]);
	pack.WriteFloat(vecAng[1]);
	pack.WriteFloat(vecAng[2]);
	pack.WriteCell(g_bSpawningExtraTank ? 1 : 0);
	CreateTimer(0.12, Timer_PostTankSpawnActions, pack, TIMER_FLAG_NO_MAPCHANGE | TIMER_DATA_HNDL_CLOSE);
}

public Action Timer_PostTankSpawnActions(Handle timer, DataPack pack)
{
	pack.Reset();
	int userid = pack.ReadCell();
	float vecPos[3], vecAng[3];
	vecPos[0] = pack.ReadFloat();
	vecPos[1] = pack.ReadFloat();
	vecPos[2] = pack.ReadFloat();
	vecAng[0] = pack.ReadFloat();
	vecAng[1] = pack.ReadFloat();
	vecAng[2] = pack.ReadFloat();
	bool wasExtraTank = (pack.ReadCell() != 0);

	if (!g_hCvarEnabled.BoolValue)
	{
		return Plugin_Stop;
	}
	if (userid > 0)
	{
		ApplyPlannedTankHPByUserId(userid);
	}

	float now = GetGameTime();
	if (!wasExtraTank && g_iPlannedExtraTanks > 0 && (now - g_fLastAdditiveTankTime >= 15.0))
	{
		g_fLastAdditiveTankTime = now;
		int extraCount = ClampInt(g_iPlannedExtraTanks, 0, 4);
		for (int idx = 1; idx <= extraCount; idx++)
		{
			DataPack extraPack = new DataPack();
			extraPack.WriteFloat(vecPos[0]);
			extraPack.WriteFloat(vecPos[1]);
			extraPack.WriteFloat(vecPos[2]);
			extraPack.WriteFloat(vecAng[0]);
			extraPack.WriteFloat(vecAng[1]);
			extraPack.WriteFloat(vecAng[2]);
			extraPack.WriteCell(idx);
			CreateTimer(0.15 * float(idx), Timer_SpawnStaggeredExtraTank, extraPack, TIMER_FLAG_NO_MAPCHANGE | TIMER_DATA_HNDL_CLOSE);
		}
	}
	return Plugin_Stop;
}

void ApplyPlannedTankHPByUserId(int userid)
{
	int tank = GetClientOfUserId(userid);
	if (tank <= 0 || tank > MaxClients || !IsClientInGame(tank) || GetClientTeam(tank) != TEAM_INFECTED || !IsPlayerAlive(tank))
	{
		return;
	}
	int targetHP = (g_iPlannedTankHP > 0) ? g_iPlannedTankHP : 8000;
	SetEntProp(tank, Prop_Data, "m_iMaxHealth", targetHP);
	SetEntProp(tank, Prop_Data, "m_iHealth", targetHP);
	SetEntityHealth(tank, targetHP);
	DirLog("TANK_HP_APPLIED: tank=%d (%N) directive=%s HP=%d", tank, tank, g_sBossName, targetHP);
}

public Action Timer_ApplyPlannedTankHP(Handle timer, int userid)
{
	ApplyPlannedTankHPByUserId(userid);
	return Plugin_Stop;
}

public Action Timer_SpawnStaggeredExtraTank(Handle timer, DataPack pack)
{
	pack.Reset();
	float basePos[3], baseAng[3];
	basePos[0] = pack.ReadFloat();
	basePos[1] = pack.ReadFloat();
	basePos[2] = pack.ReadFloat();
	baseAng[0] = pack.ReadFloat();
	baseAng[1] = pack.ReadFloat();
	baseAng[2] = pack.ReadFloat();
	int tankIndex = pack.ReadCell();

	if (!g_hCvarEnabled.BoolValue || !HasSafeClientSlotMargin(1))
	{
		return Plugin_Stop;
	}

	float spawnPos[3], sign = (tankIndex % 2 == 1) ? 1.0 : -1.0;
	spawnPos[0] = basePos[0] + (120.0 * sign);
	spawnPos[1] = basePos[1] + (120.0 * sign);
	spawnPos[2] = basePos[2] + 15.0;

	if (!IsHullClearForSI(spawnPos))
	{
		int anchor = ResolveAliveSurvivor(0);
		if (!IsValidAliveSurvivor(anchor) || !FindTier2AnySurvivorPZPos(anchor, ZC_TANK, spawnPos))
		{
			spawnPos[0] = basePos[0];
			spawnPos[1] = basePos[1];
			spawnPos[2] = basePos[2] + 25.0;
		}
	}

	g_bSpawningExtraTank = true;
	g_bOurSpawn = true;
	int extraTank = L4D2_SpawnTank(spawnPos, baseAng);
	g_bOurSpawn = false;
	if (extraTank <= 0 || !IsClientInGame(extraTank))
	{
		extraTank = SpawnInfectedBotViaSDKCall(ZC_TANK, spawnPos, baseAng);
		if (extraTank > 0)
		{
			CreateTimer(0.1, Timer_ApplyPlannedTankHP, GetClientUserId(extraTank), TIMER_FLAG_NO_MAPCHANGE);
		}
	}
	g_bSpawningExtraTank = false;

	DirLog("EXTRA_TANK_SPAWN #%d (%s): client=%d", tankIndex, g_sBossName, extraTank);
	if (extraTank > 0 && g_hCvarChatReport.BoolValue)
	{
		PrintToChatAll("\x04[AI Director Online]\x01 Boss 指令 (\x03%s\x01) — 增援第 \x05#%d\x01 隻 Tank！", g_sBossName, tankIndex + 1);
	}
	return Plugin_Stop;
}

public Action L4D2_OnChooseVictim(int specialInfected, int &curTarget)
{
	if (!g_hCvarEnabled.BoolValue || !g_bLeftSafeArea || !g_bFocusStrike ||
		specialInfected <= 0 || specialInfected > MaxClients || !IsClientInGame(specialInfected) ||
		!IsPlayerAlive(specialInfected) || g_bIsFrozenAmbush[specialInfected] || !IsValidAliveSurvivor(g_iTargetSurvivor))
	{
		return Plugin_Continue;
	}

	int newTarget = curTarget;
	bool changed = false;
	if (g_bIsFocusSquad[specialInfected])
	{
		if (curTarget != g_iTargetSurvivor)
		{
			newTarget = g_iTargetSurvivor;
			changed = true;
		}
	}
	else if (curTarget == g_iTargetSurvivor)
	{
		int altSurvivor = FindNearestOtherSurvivor(specialInfected, g_iTargetSurvivor);
		if (altSurvivor > 0)
		{
			newTarget = altSurvivor;
			changed = true;
		}
	}

	if (changed && IsValidAliveSurvivor(newTarget))
	{
		curTarget = newTarget;
		float now = GetGameTime();
		if (g_hCvarVerboseHookLog.BoolValue && now - g_fLastChooseVictimLogTime[specialInfected] >= 5.0)
		{
			g_fLastChooseVictimLogTime[specialInfected] = now;
			HookLog("L4D2_OnChooseVictim", "SI=%N Focus=%d -> %N",
				specialInfected, g_bIsFocusSquad[specialInfected] ? 1 : 0, curTarget);
		}
		return Plugin_Changed;
	}
	return Plugin_Continue;
}

// ============================================================================
// Main 1-Second State Machine & Spatial Flow/Stagnation Sampler
// ============================================================================

public Action Timer_MainDirectorTick(Handle timer)
{
	if (!g_hCvarEnabled.BoolValue || !g_bLeftSafeArea)
	{
		return Plugin_Continue;
	}

	SampleTeamFlowAndStagnation();

	float now = GetGameTime(), stateElapsed = now - g_fStateEnterTime;
	if (now - g_fLastHeartbeatTime >= g_hCvarTickInterval.FloatValue)
	{
		g_fLastHeartbeatTime = now;
		SendHeartbeatTickNotify();
	}

	switch (g_eState)
	{
		case STATE_COOLDOWN:
		{
			if (g_bSuppressCooldownMobs) L4D_ResetMobTimer();
			if (stateElapsed >= float(g_iCurrentCooldown))
			{
				ChangeState(STATE_EVALUATE, "Cooldown elapsed -> RPC EvaluateWave");
				RequestWaveEvaluationFromPython("CooldownTimerElapsed");
			}
		}
		case STATE_EVALUATE:
		{
			if (!g_bWaitingForEvalReply) RequestWaveEvaluationFromPython("EvaluateRetry");
		}
		case STATE_SPAWNING:
		{
			UpdateFrozenAmbushSI();
			UpdateFocusStrikeEnforcement();
			if (stateElapsed > 20.0)
			{
				delete g_hSpawnQueueTimer;
				ChangeState(STATE_COMBAT, "Spawning safety timeout");
			}
		}
		case STATE_COMBAT:
		{
			UpdateFrozenAmbushSI();
			UpdateFocusStrikeEnforcement();
			int activeSI = CountActiveNonTankSI();
			if (activeSI == 0)
			{
				FinishCombatWave("All active SI eliminated");
			}
			else if (stateElapsed >= g_fCombatTimeout)
			{
				int recycled = RecycleStuckOrFarSI();
				DirLog("STATE_COMBAT timeout: activeSI=%d recycled=%d", activeSI, recycled);
				FinishCombatWave("Combat timeout");
			}
		}
	}
	return Plugin_Continue;
}

void SampleTeamFlowAndStagnation()
{
	float maxFlowDist = L4D2Direct_GetMapMaxFlowDistance();
	if (maxFlowDist <= 1.0) maxFlowDist = 1.0;

	float highestFlow = 0.0, centroid[3];
	int aliveCount = 0;
	for (int i = 1; i <= MaxClients; i++)
	{
		if (!IsValidAliveSurvivor(i)) continue;
		aliveCount++;
		float flow = L4D2Direct_GetFlowDistance(i);
		if (flow > highestFlow) highestFlow = flow;
		float pos[3];
		GetClientAbsOrigin(i, pos);
		centroid[0] += pos[0];
		centroid[1] += pos[1];
		centroid[2] += pos[2];
	}
	if (aliveCount == 0) return;

	centroid[0] /= float(aliveCount);
	centroid[1] /= float(aliveCount);
	centroid[2] /= float(aliveCount);

	float curFlowRatio = (highestFlow / maxFlowDist) * 100.0;
	if (curFlowRatio > g_fPeakFlowRatio) g_fPeakFlowRatio = curFlowRatio;

	float now = GetGameTime(), dt = now - g_fLastFlowSampleTime;
	if (dt >= 5.0)
	{
		g_fFlowVelocity = ((g_fPeakFlowRatio - g_fPrevPeakFlowRatio) / dt) * 60.0;
		g_fPrevPeakFlowRatio = g_fPeakFlowRatio;
		g_fLastFlowSampleTime = now;
	}

	if (GetVectorDistance(centroid, g_fStagnationAnchor) <= 250.0)
	{
		g_fStagnationSeconds += 1.0;
	}
	else
	{
		g_fStagnationSeconds = 0.0;
		g_fStagnationAnchor[0] = centroid[0];
		g_fStagnationAnchor[1] = centroid[1];
		g_fStagnationAnchor[2] = centroid[2];
	}
}

// ============================================================================
// Wave Spawn Queue & Combat Enforcement (Actuator)
// ============================================================================

void ExecuteUncommonSideEffects(int preferredAnchor, bool spawnRiotSquad, bool spawnMercyClown)
{
	int anchor = ResolveAliveSurvivor(preferredAnchor);
	if (!IsValidAliveSurvivor(anchor)) return;

	if (spawnRiotSquad)
	{
		for (int i = 0; i < 4; i++)
		{
			ExecuteCheatCommand(anchor, "z_spawn_old", (i % 2 == 0) ? "riot auto" : "ceda auto");
		}
	}
	else if (spawnMercyClown)
	{
		ExecuteCheatCommand(anchor, "z_spawn_old", "clown auto");
	}
}

public Action Timer_SpawnQueueTick(Handle timer)
{
	if (g_eState != STATE_SPAWNING || !g_hCvarEnabled.BoolValue || !g_bLeftSafeArea)
	{
		g_hSpawnQueueTimer = null;
		return Plugin_Stop;
	}
	if (g_iWaveCursor >= g_iWaveTargetCount || !HasSafeClientSlotMargin(1))
	{
		g_hSpawnQueueTimer = null;
		OnWaveSpawningFinished();
		return Plugin_Stop;
	}

	int zc = ClampInt(g_iWaveQueue[g_iWaveCursor], ZC_SMOKER, ZC_CHARGER);
	bool assignFocusSquad = (g_bFocusStrike && g_iWaveCursor < g_iPlannedFocusSquadSize);
	g_iWaveCursor++;

	if (SpawnSpecialInfectedMultiTier(zc, g_iTargetSurvivor, g_eSpawnDir, assignFocusSquad) > 0)
	{
		g_iWaveSuccessCount++;
	}

	if (g_iWaveCursor >= g_iWaveTargetCount)
	{
		g_hSpawnQueueTimer = null;
		OnWaveSpawningFinished();
		return Plugin_Stop;
	}
	return Plugin_Continue;
}

void OnWaveSpawningFinished()
{
	DirLog("WAVE_SPAWN_DONE [W#%d]: %d / %d SI", g_iWaveNumber, g_iWaveSuccessCount, g_iWaveTargetCount);
	if (g_iWaveSuccessCount == 0)
	{
		g_fWaveCombatStartTime = 0.0;
		g_iCurrentCooldown = 5;
		ChangeState(STATE_COOLDOWN, "0 SI spawned -> 5s retry");
		return;
	}
	if (GetFeatureStatus(FeatureType_Native, "L4D2_StartAssault") == FeatureStatus_Available)
	{
		L4D2_StartAssault();
	}
	if (CountActiveNonTankSI() == 0)
	{
		g_fLastWaveClearDuration = FloatMax(0.1, (g_fWaveCombatStartTime > 0.0) ? (GetGameTime() - g_fWaveCombatStartTime) : 1.0);
		g_fWaveCombatStartTime = 0.0;
		SendDirectorEventNotify("wave_cleared", "speed_wiped_during_spawn");
		ChangeState(STATE_COOLDOWN, "Wave speed-wiped during spawn");
		return;
	}
	g_fWaveCombatStartTime = GetGameTime();
	g_fCombatTimeout = 28.0;
	ChangeState(STATE_COMBAT, "Wave spawning complete");
}

void FinishCombatWave(const char[] reason)
{
	for (int i = 1; i <= MaxClients; i++)
	{
		if (g_bIsFrozenAmbush[i]) UnfreezeSI(i, "CombatWaveEnd");
		g_bIsFocusSquad[i] = false;
	}

	if (g_fWaveCombatStartTime > 0.0)
	{
		g_fLastWaveClearDuration = (CountActiveNonTankSI() == 0) ? FloatMax(0.1, GetGameTime() - g_fWaveCombatStartTime) : g_fCombatTimeout;
		g_fWaveCombatStartTime = 0.0;
	}

	SendDirectorEventNotify("wave_finished", reason);
	ChangeState(STATE_COOLDOWN, reason);
}

void UpdateFrozenAmbushSI()
{
	float now = GetGameTime();
	for (int i = 1; i <= MaxClients; i++)
	{
		if (!g_bIsFrozenAmbush[i]) continue;
		if (!IsClientInGame(i) || GetClientTeam(i) != TEAM_INFECTED || !IsPlayerAlive(i))
		{
			g_bIsFrozenAmbush[i] = false;
			continue;
		}
		if (now >= g_fFreezeExpireTime[i])                UnfreezeSI(i, "TimerExpired");
		else if (GetNearestSurvivorDistance(i) <= 550.0) UnfreezeSI(i, "Proximity<=550");
		else if (IsClientVisibleToAnySurvivor(i))        UnfreezeSI(i, "SurvivorLOS");
	}
}

void UnfreezeSI(int bot, const char[] reason)
{
	g_bIsFrozenAmbush[bot] = false;
	if (g_bWaveWaitingForAmbush)
	{
		g_bWaveWaitingForAmbush = false;
		if (g_fWaveCombatStartTime > 0.0) g_fWaveCombatStartTime = GetGameTime();
	}
	if (IsClientInGame(bot))
	{
		SetEntityFlags(bot, GetEntityFlags(bot) & ~FL_FROZEN);
		if (IsPlayerAlive(bot))
		{
			if (GetFeatureStatus(FeatureType_Native, "L4D2_StartAssault") == FeatureStatus_Available) L4D2_StartAssault();
			if (g_bIsFocusSquad[bot] && IsValidAliveSurvivor(g_iTargetSurvivor)) L4D2_CommandABot(bot, g_iTargetSurvivor, BOT_CMD_ATTACK);
		}
		DirLog("AMBUSH_RELEASE: %N (%s)", bot, reason);
	}
}

void UpdateFocusStrikeEnforcement()
{
	if (!g_bFocusStrike || !IsValidAliveSurvivor(g_iTargetSurvivor))
	{
		return;
	}

	int curFocus = 0, limit = (g_iPlannedFocusSquadSize > 0) ? g_iPlannedFocusSquadSize : 1;
	for (int i = 1; i <= MaxClients; i++)
	{
		if (!g_bIsFocusSquad[i]) continue;
		if (!IsClientInGame(i) || GetClientTeam(i) != TEAM_INFECTED || !IsPlayerAlive(i))
		{
			g_bIsFocusSquad[i] = false;
			continue;
		}
		curFocus++;
		if (curFocus > limit)
		{
			g_bIsFocusSquad[i] = false;
			int alt = FindNearestOtherSurvivor(i, g_iTargetSurvivor);
			if (alt > 0) L4D2_CommandABot(i, alt, BOT_CMD_ATTACK);
		}
	}

	if (IsSurvivorPinned(g_iTargetSurvivor) || IsSurvivorIncapOrHanging(g_iTargetSurvivor))
	{
		return;
	}

	for (int i = 1; i <= MaxClients; i++)
	{
		if (!g_bIsFocusSquad[i] || g_bIsFrozenAmbush[i] || !IsClientInGame(i) || GetClientTeam(i) != TEAM_INFECTED || !IsPlayerAlive(i))
		{
			continue;
		}
		L4D2_CommandABot(i, g_iTargetSurvivor, BOT_CMD_ATTACK);
	}
}

int RecycleStuckOrFarSI()
{
	int count = 0;
	for (int i = 1; i <= MaxClients; i++)
	{
		if (!IsClientInGame(i) || !IsFakeClient(i) || GetClientTeam(i) != TEAM_INFECTED || !IsPlayerAlive(i)) continue;
		if (GetEntProp(i, Prop_Send, "m_zombieClass") == ZC_TANK || IsSIPinningAnySurvivor(i)) continue;
		if (GetNearestSurvivorDistance(i) > 1800.0 && !IsClientVisibleToAnySurvivor(i))
		{
			ForcePlayerSuicide(i);
			count++;
		}
	}
	return count;
}

// ============================================================================
// Multi-Tier SI Spawning Pipeline (NavMesh / LOS / SafeRoom Edge Guards)
// ============================================================================

int SpawnSpecialInfectedMultiTier(int zc, int preferredAnchor, SpawnDirection dir, bool isFocusSquad)
{
	if (!HasSafeClientSlotMargin(1) || CountActiveNonTankSI() >= MAX_SI_SLOTS - 2)
	{
		return 0;
	}
	int anchor = ResolveAliveSurvivor(preferredAnchor);
	if (!IsValidAliveSurvivor(anchor))
	{
		return 0;
	}

	float spawnPos[3], spawnAng[3];
	int tierUsed = 0;
	if (FindTier1ScoredSpawnPos(anchor, zc, dir, spawnPos))       tierUsed = 1;
	else if (FindTier2AnySurvivorPZPos(anchor, zc, spawnPos))     tierUsed = 2;
	else if (FindTier3RadialNavPos(anchor, dir, spawnPos))        tierUsed = 3;

	int bot = 0;
	if (tierUsed > 0)
	{
		float anchorPos[3];
		GetClientAbsOrigin(anchor, anchorPos);
		MakeVectorFromPoints(spawnPos, anchorPos, spawnAng);
		GetVectorAngles(spawnAng, spawnAng);
		spawnAng[0] = 0.0;
		spawnAng[2] = 0.0;

		if (g_bSDKCallsReady) bot = SpawnInfectedBotViaSDKCall(zc, spawnPos, spawnAng);
		if (bot <= 0 || !IsClientInGame(bot) || !IsPlayerAlive(bot))
		{
			g_bOurSpawn = true;
			bot = L4D2_SpawnSpecial(zc, spawnPos, spawnAng);
			g_bOurSpawn = false;
		}
	}

	if (bot <= 0 || !IsClientInGame(bot) || !IsPlayerAlive(bot))
	{
		bot = SpawnViaZSpawnOldAuto(anchor, zc);
		if (bot > 0 && IsClientInGame(bot) && IsPlayerAlive(bot))
		{
			if (tierUsed > 0) TeleportEntity(bot, spawnPos, spawnAng, NULL_VECTOR);
			else { tierUsed = 4; GetClientAbsOrigin(bot, spawnPos); }
		}
	}

	if (bot > 0 && IsClientInGame(bot) && IsPlayerAlive(bot))
	{
		g_bIsFocusSquad[bot]   = isFocusSquad;
		g_bIsFrozenAmbush[bot] = false;
		float anchorPos[3];
		GetClientAbsOrigin(anchor, anchorPos);
		float dist = GetVectorDistance(spawnPos, anchorPos);
		if (g_bFreezeAmbush && dist > 380.0 && !IsClientVisibleToAnySurvivor(bot))
		{
			g_bIsFrozenAmbush[bot]  = true;
			g_bWaveWaitingForAmbush = true;
			g_fFreezeExpireTime[bot] = GetGameTime() + 8.0;
			SetEntityFlags(bot, GetEntityFlags(bot) | FL_FROZEN);
		}
		else if (isFocusSquad && IsValidAliveSurvivor(g_iTargetSurvivor))
		{
			L4D2_CommandABot(bot, g_iTargetSurvivor, BOT_CMD_ATTACK);
		}
		DirLog("SPAWN_SI [W#%d]: %s(%d) Tier=%d Focus=%d Freeze=%d dist=%.0f",
			g_iWaveNumber, g_sSINames[zc], zc, tierUsed, isFocusSquad ? 1 : 0, g_bIsFrozenAmbush[bot] ? 1 : 0, dist);
		return bot;
	}
	return 0;
}

bool FindTier1ScoredSpawnPos(int anchor, int zc, SpawnDirection dir, float outPos[3])
{
	float anchorPos[3], anchorEyeAng[3], anchorFwd[3];
	GetClientAbsOrigin(anchor, anchorPos);
	GetClientEyeAngles(anchor, anchorEyeAng);
	anchorEyeAng[0] = 0.0;
	anchorEyeAng[2] = 0.0;
	GetAngleVectors(anchorEyeAng, anchorFwd, NULL_VECTOR, NULL_VECTOR);
	NormalizeVector(anchorFwd, anchorFwd);
	float anchorFlow = L4D2Direct_GetFlowDistance(anchor);

	float bestScore = -999999.0;
	bool found = false;
	for (int i = 0; i < 12; i++)
	{
		float cand[3];
		if (!L4D_GetRandomPZSpawnPosition(anchor, zc, 5, cand)) continue;
		float dist = GetVectorDistance(anchorPos, cand);
		if (dist < 180.0 || dist > SPAWN_DIST_MAX || IsPointVisibleToAnySurvivor(cand)) continue;

		float score = ScoreCandidatePosition(anchorPos, anchorFwd, anchorFlow, cand, dist, dir);
		if (score > bestScore)
		{
			bestScore = score;
			outPos[0] = cand[0]; outPos[1] = cand[1]; outPos[2] = cand[2];
			found = true;
		}
	}
	return found;
}

float ScoreCandidatePosition(const float anchorPos[3], const float anchorFwd[3], float anchorFlow, float candPos[3], float dist, SpawnDirection dir)
{
	float score = 100.0;
	if (dist >= SPAWN_DIST_MIN && dist <= 900.0) score += 35.0;
	else if (dist > 900.0)                       score -= (dist - 900.0) * 0.05;

	float dz = candPos[2] - anchorPos[2];
	float toCand[3];
	toCand[0] = candPos[0] - anchorPos[0];
	toCand[1] = candPos[1] - anchorPos[1];
	toCand[2] = 0.0;
	NormalizeVector(toCand, toCand);
	float dot = GetVectorDotProduct(anchorFwd, toCand);

	Address navArea = L4D2Direct_GetTerrorNavArea(candPos);
	float candFlow = anchorFlow;
	if (navArea != Address_Null)
	{
		candFlow = L4D2Direct_GetTerrorNavAreaFlow(navArea);
		if (L4D_GetNavArea_SpawnAttributes(navArea) & NAV_SPAWN_OBSCURED) score += 20.0;
	}

	switch (dir)
	{
		case DIR_FRONT:        { if (candFlow > anchorFlow + 50.0) score += 45.0; if (dot > 0.35) score += 35.0; else if (dot < -0.2) score -= 40.0; }
		case DIR_MIDDLE_FLANK: { if (FloatAbs(dot) <= 0.65) score += 50.0; else score -= 25.0; }
		case DIR_BEHIND:       { if (candFlow < anchorFlow - 50.0) score += 45.0; if (dot < -0.30) score += 35.0; else if (dot > 0.25) score -= 40.0; }
		case DIR_ABOVE_ROOF:   { if (dz >= SPAWN_HEIGHT_THRESHOLD) score += 70.0; else if (dz >= 45.0) score += 25.0; else score -= 30.0; }
		case DIR_BELOW_FLOOR:  { if (dz <= -SPAWN_HEIGHT_THRESHOLD) score += 70.0; else if (dz <= -45.0) score += 25.0; else score -= 30.0; }
	}
	return score;
}

bool FindTier2AnySurvivorPZPos(int primaryAnchor, int zc, float outPos[3])
{
	if (L4D_GetRandomPZSpawnPosition(primaryAnchor, zc, 20, outPos)) return true;
	int ahead = L4D_GetHighestFlowSurvivor();
	if (IsValidAliveSurvivor(ahead) && ahead != primaryAnchor && L4D_GetRandomPZSpawnPosition(ahead, zc, 20, outPos)) return true;

	for (int i = 1; i <= MaxClients; i++)
	{
		if (IsValidAliveSurvivor(i) && i != primaryAnchor && i != ahead && L4D_GetRandomPZSpawnPosition(i, zc, 15, outPos)) return true;
	}
	return (zc != ZC_HUNTER && L4D_GetRandomPZSpawnPosition(primaryAnchor, ZC_HUNTER, 15, outPos));
}

bool FindTier3RadialNavPos(int primaryAnchor, SpawnDirection dir, float outPos[3])
{
	float anchorPos[3], eyeAng[3];
	GetClientAbsOrigin(primaryAnchor, anchorPos);
	GetClientEyeAngles(primaryAnchor, eyeAng);

	float baseYaw = eyeAng[1];
	if (dir == DIR_BEHIND)            baseYaw += 180.0;
	else if (dir == DIR_MIDDLE_FLANK) baseYaw += (GetRandomInt(0, 1) == 0) ? 90.0 : -90.0;

	static const float radii[4]    = { 450.0, 650.0, 850.0, 350.0 };
	static const float zOffsets[3] = { 120.0, 260.0, -40.0 };

	for (int r = 0; r < sizeof(radii); r++)
	{
		for (int step = 0; step < 12; step++)
		{
			float rad = DegToRad(baseYaw + float(step * 30));
			for (int zIdx = 0; zIdx < sizeof(zOffsets); zIdx++)
			{
				float startPt[3], endPt[3], groundPt[3];
				startPt[0] = anchorPos[0] + Cosine(rad) * radii[r];
				startPt[1] = anchorPos[1] + Sine(rad)   * radii[r];
				startPt[2] = anchorPos[2] + zOffsets[zIdx];
				endPt[0] = startPt[0]; endPt[1] = startPt[1]; endPt[2] = anchorPos[2] - 400.0;

				Handle trace = TR_TraceRayFilterEx(startPt, endPt, MASK_PLAYERSOLID_BRUSHONLY, RayType_EndPoint, TraceFilter_WorldOnly);
				bool hit = TR_DidHit(trace);
				if (hit) TR_GetEndPosition(groundPt, trace);
				delete trace;
				if (!hit) continue;

				groundPt[2] += 10.0;
				Address navArea = L4D2Direct_GetTerrorNavArea(groundPt);
				if (navArea == Address_Null || (L4D_GetNavArea_SpawnAttributes(navArea) & NAV_SPAWN_CHECKPOINT)) continue;
				if (!IsHullClearForSI(groundPt) || IsTooCloseOrVisibleToSurvivors(groundPt, 220.0)) continue;

				outPos[0] = groundPt[0]; outPos[1] = groundPt[1]; outPos[2] = groundPt[2];
				return true;
			}
		}
	}
	return false;
}

int SpawnViaZSpawnOldAuto(int survivorClient, int zc)
{
	if (zc < 1 || zc > 6 || !IsValidAliveSurvivor(survivorClient)) return 0;

	bool existedBefore[MAXPLAYERS + 1];
	for (int i = 1; i <= MaxClients; i++)
	{
		existedBefore[i] = (IsClientInGame(i) && GetClientTeam(i) == TEAM_INFECTED && IsPlayerAlive(i));
	}

	char args[64];
	FormatEx(args, sizeof(args), "%s auto", g_sSICmdNames[zc]);
	g_bOurSpawn = true;
	ExecuteCheatCommand(survivorClient, "z_spawn_old", args);
	g_bOurSpawn = false;

	for (int i = 1; i <= MaxClients; i++)
	{
		if (!existedBefore[i] && IsClientInGame(i) && !IsClientInKickQueue(i) && GetClientTeam(i) == TEAM_INFECTED && IsPlayerAlive(i))
		{
			return i;
		}
	}

	if (HasSafeClientSlotMargin(2))
	{
		int tempBot = CreateFakeClient("DirTempSI");
		if (tempBot > 0 && IsClientInGame(tempBot))
		{
			ChangeClientTeam(tempBot, TEAM_INFECTED);
			FormatEx(args, sizeof(args), "%s auto area", g_sSICmdNames[zc]);
			g_bOurSpawn = true;
			ExecuteCheatCommand(tempBot, "z_spawn_old", args);
			g_bOurSpawn = false;
			KickClient(tempBot, "Temp SI spawner cleanup");

			for (int i = 1; i <= MaxClients; i++)
			{
				if (i != tempBot && !existedBefore[i] && IsClientInGame(i) && !IsClientInKickQueue(i) && GetClientTeam(i) == TEAM_INFECTED && IsPlayerAlive(i))
				{
					return i;
				}
			}
		}
	}
	return 0;
}

void ExecuteCheatCommand(int client, const char[] command, const char[] args)
{
	int flags = GetCommandFlags(command);
	SetCommandFlags(command, flags & ~FCVAR_CHEAT);
	FakeClientCommand(client, "%s %s", command, args);
	SetCommandFlags(command, flags);
}

// ============================================================================
// Geometry, Visibility & Safety Utilities
// ============================================================================

int ClampInt(int val, int minVal, int maxVal)
{
	if (val < minVal) return minVal;
	if (val > maxVal) return maxVal;
	return val;
}

float FloatMax(float a, float b)
{
	return (a >= b) ? a : b;
}

bool HasSafeClientSlotMargin(int neededSpawns)
{
	return (GetClientCount(false) + neededSpawns <= SAFE_CLIENT_SLOT_LIMIT);
}

bool IsValidAliveSurvivor(int client)
{
	return (client > 0 && client <= MaxClients && IsClientInGame(client) && GetClientTeam(client) == TEAM_SURVIVOR && IsPlayerAlive(client));
}

int ResolveAliveSurvivor(int preferred)
{
	if (IsValidAliveSurvivor(preferred)) return preferred;
	int ahead = L4D_GetHighestFlowSurvivor();
	if (IsValidAliveSurvivor(ahead)) return ahead;
	for (int i = 1; i <= MaxClients; i++)
	{
		if (IsValidAliveSurvivor(i)) return i;
	}
	return 0;
}

int CountAliveSurvivors()
{
	int count = 0;
	for (int i = 1; i <= MaxClients; i++)
	{
		if (IsValidAliveSurvivor(i)) count++;
	}
	return count;
}

bool IsSurvivorIncapOrHanging(int client)
{
	return (GetEntProp(client, Prop_Send, "m_isIncapacitated", 1) != 0 || GetEntProp(client, Prop_Send, "m_isHangingFromLedge", 1) != 0);
}

bool IsSurvivorPinned(int client)
{
	return IsValidAliveSurvivor(client) && (
		GetEntPropEnt(client, Prop_Send, "m_pounceAttacker") > 0 ||
		GetEntPropEnt(client, Prop_Send, "m_tongueOwner")    > 0 ||
		GetEntPropEnt(client, Prop_Send, "m_jockeyAttacker") > 0 ||
		GetEntPropEnt(client, Prop_Send, "m_pummelAttacker") > 0 ||
		GetEntPropEnt(client, Prop_Send, "m_carryAttacker")  > 0);
}

bool IsSIPinningAnySurvivor(int siClient)
{
	for (int i = 1; i <= MaxClients; i++)
	{
		if (!IsValidAliveSurvivor(i)) continue;
		if (GetEntPropEnt(i, Prop_Send, "m_pounceAttacker") == siClient ||
			GetEntPropEnt(i, Prop_Send, "m_tongueOwner")    == siClient ||
			GetEntPropEnt(i, Prop_Send, "m_jockeyAttacker") == siClient ||
			GetEntPropEnt(i, Prop_Send, "m_pummelAttacker") == siClient ||
			GetEntPropEnt(i, Prop_Send, "m_carryAttacker")  == siClient)
		{
			return true;
		}
	}
	return false;
}

int FindNearestOtherSurvivor(int fromClient, int excludeSurvivor)
{
	float fromPos[3], bestDist = 999999.0;
	GetClientAbsOrigin(fromClient, fromPos);
	int bestClient = 0;
	for (int i = 1; i <= MaxClients; i++)
	{
		if (!IsValidAliveSurvivor(i) || i == excludeSurvivor) continue;
		float pos[3];
		GetClientAbsOrigin(i, pos);
		float d = GetVectorDistance(fromPos, pos);
		if (d < bestDist) { bestDist = d; bestClient = i; }
	}
	return bestClient;
}

float GetNearestSurvivorDistance(int fromClient)
{
	float fromPos[3], minDist = 999999.0;
	GetClientAbsOrigin(fromClient, fromPos);
	for (int i = 1; i <= MaxClients; i++)
	{
		if (!IsValidAliveSurvivor(i)) continue;
		float pos[3];
		GetClientAbsOrigin(i, pos);
		float d = GetVectorDistance(fromPos, pos);
		if (d < minDist) minDist = d;
	}
	return minDist;
}

int CountActiveNonTankSI()
{
	int count = 0;
	for (int i = 1; i <= MaxClients; i++)
	{
		if (IsClientInGame(i) && GetClientTeam(i) == TEAM_INFECTED && IsPlayerAlive(i))
		{
			int zc = GetEntProp(i, Prop_Send, "m_zombieClass");
			if (zc >= ZC_SMOKER && zc <= ZC_CHARGER) count++;
		}
	}
	return count;
}

bool IsTankAliveInPlay()
{
	if (L4D2_IsTankInPlay() || L4D2_GetTankCount() > 0) return true;
	for (int i = 1; i <= MaxClients; i++)
	{
		if (IsClientInGame(i) && GetClientTeam(i) == TEAM_INFECTED && IsPlayerAlive(i) &&
			GetEntProp(i, Prop_Send, "m_zombieClass") == ZC_TANK)
		{
			return true;
		}
	}
	return false;
}

bool IsAnySurvivorOutsideSafeRoom()
{
	if (!L4D_HasMapStarted()) return false;
	if (L4D_HasAnySurvivorLeftSafeArea()) return true;
	for (int i = 1; i <= MaxClients; i++)
	{
		if (!IsValidAliveSurvivor(i)) continue;
		Address nav = L4D_GetLastKnownArea(i);
		if (nav != Address_Null && !(L4D_GetNavArea_SpawnAttributes(nav) & (NAV_SPAWN_CHECKPOINT | NAV_SPAWN_PLAYER_START)))
		{
			return true;
		}
	}
	return false;
}

bool IsPointVisibleToAnySurvivor(const float pt[3])
{
	float targetChest[3];
	targetChest[0] = pt[0]; targetChest[1] = pt[1]; targetChest[2] = pt[2] + 45.0;
	for (int i = 1; i <= MaxClients; i++)
	{
		if (!IsValidAliveSurvivor(i)) continue;
		float eyePos[3];
		GetClientEyePosition(i, eyePos);
		if (GetVectorDistance(eyePos, targetChest) > 2000.0) continue;
		Handle trace = TR_TraceRayFilterEx(eyePos, targetChest, MASK_VISIBLE, RayType_EndPoint, TraceFilter_NoPlayers);
		bool hit = TR_DidHit(trace);
		delete trace;
		if (!hit) return true;
	}
	return false;
}

bool IsClientVisibleToAnySurvivor(int client)
{
	float pos[3];
	GetClientAbsOrigin(client, pos);
	return IsPointVisibleToAnySurvivor(pos);
}

bool IsTooCloseOrVisibleToSurvivors(const float pt[3], float minAllowedDist)
{
	for (int i = 1; i <= MaxClients; i++)
	{
		if (!IsValidAliveSurvivor(i)) continue;
		float survPos[3];
		GetClientAbsOrigin(i, survPos);
		if (GetVectorDistance(pt, survPos) < minAllowedDist) return true;
	}
	return IsPointVisibleToAnySurvivor(pt);
}

bool IsHullClearForSI(const float pos[3])
{
	static const float mins[3] = { -16.0, -16.0, 0.0 };
	static const float maxs[3] = {  16.0,  16.0, 72.0 };
	Handle trace = TR_TraceHullFilterEx(pos, pos, mins, maxs, MASK_PLAYERSOLID, TraceFilter_NoPlayers);
	bool hit = TR_DidHit(trace);
	delete trace;
	return !hit;
}

public bool TraceFilter_WorldOnly(int entity, int contentsMask) { return (entity == 0); }
public bool TraceFilter_NoPlayers(int entity, int contentsMask) { return (entity <= 0 || entity > MaxClients); }

// ============================================================================
// Admin Commands
// ============================================================================

public Action Cmd_DirStatus(int client, int args)
{
	char rpcServer[64];
	g_hCvarRpcServer.GetString(rpcServer, sizeof(rpcServer));
	ReplyToCommand(client, "=== [L4D2 AI Director Online v%s] ===", PLUGIN_VERSION);
	ReplyToCommand(client, "RPC: %s (port=%d) | Strat=%s | WaitEval=%d | SDKCalls=%d",
		rpcServer, RPCGetServerPort(rpcServer), g_sLastStrategyUsed, g_bWaitingForEvalReply ? 1 : 0, g_bSDKCallsReady ? 1 : 0);
	ReplyToCommand(client, "State=%s W#%d AliveSurv=%d ActiveSI=%d Clients=%d/%d | Flow=%.1f%% Vel=%.1f%%/m Stag=%.0fs Wipes=%d Stage=%d",
		g_sStateNames[g_eState], g_iWaveNumber, CountAliveSurvivors(), CountActiveNonTankSI(),
		GetClientCount(false), SAFE_CLIENT_SLOT_LIMIT, g_fPeakFlowRatio, g_fFlowVelocity, g_fStagnationSeconds, g_iWipeCount, g_iNativeFinaleStage);
	ReplyToCommand(client, "LastPlan: Diag=%s Intent=%s Role=%s Dir=%s Horde=%s(%.2fx) Boss=%s(HP=%d,+%d)",
		g_sDiagName, g_sIntentName, g_sRoleName, g_sDirNames[g_eSpawnDir], g_sHordeName, g_fPlannedHordeMult,
		g_sBossName, g_iPlannedTankHP, g_iPlannedExtraTanks);
	return Plugin_Handled;
}

public Action Cmd_DirWave(int client, int args)
{
	if (!g_bLeftSafeArea) ActivateDirector(1.0, "Cmd_DirWave");
	ChangeState(STATE_EVALUATE, "Manual sm_dironline_wave");
	RequestWaveEvaluationFromPython("Cmd_DirWave", true);
	ReplyToCommand(client, "[AI Director Online] Requested EvaluateWave from Python (seq=%d).", g_iCurrentEvalSeq);
	return Plugin_Handled;
}

public Action Cmd_DirSpawnSI(int client, int args)
{
	int zc = ZC_HUNTER;
	if (args >= 1)
	{
		char arg[16];
		GetCmdArg(1, arg, sizeof(arg));
		zc = ClampInt(StringToInt(arg), ZC_SMOKER, ZC_CHARGER);
	}
	int anchor = ResolveAliveSurvivor(client);
	int bot = SpawnSpecialInfectedMultiTier(zc, anchor, DIR_FRONT, false);
	ReplyToCommand(client, "[AI Director Online] Spawned %s(%d) -> bot=%d", g_sSINames[zc], zc, bot);
	return Plugin_Handled;
}

void SendSimpleRpcAdminCall(int client, const char[] method, const char[] key, const char[] val, RPCReplyCallback callback)
{
	char rpcServer[64];
	if (!IsRpcTargetReady(rpcServer, sizeof(rpcServer)))
	{
		ReplyToCommand(client, "[AI Director Online] RPC server '%s' is not configured.", rpcServer);
		return;
	}
	JSON j = new JSON();
	j.SetString(key, val);
	RPCCall call = new RPCCall();
	call.SetMethod(method);
	call.SetParams(j);
	j.Close();
	call.Send(rpcServer, callback);
}

public Action Cmd_SetStrategy(int client, int args)
{
	if (args < 1)
	{
		ReplyToCommand(client, "Usage: sm_dironline_strategy <strategy_name>");
		return Plugin_Handled;
	}
	char stratName[64];
	GetCmdArg(1, stratName, sizeof(stratName));
	SendSimpleRpcAdminCall(client, "SetStrategy", "strategy", stratName, OnRpcSetStrategyReply);
	ReplyToCommand(client, "[AI Director Online] Switching Python strategy -> %s", stratName);
	return Plugin_Handled;
}

public void OnRpcSetStrategyReply(JSON result)
{
	bool ok = result.GetBool("ok");
	char msg[256], strat[64];
	result.GetString("message", msg, sizeof(msg));
	result.GetString("strategy", strat, sizeof(strat));
	result.Close();
	if (ok) strcopy(g_sLastStrategyUsed, sizeof(g_sLastStrategyUsed), strat);
	DirLog("SetStrategy: ok=%d strat=%s (%s)", ok ? 1 : 0, strat, msg);
	PrintToChatAll("\x04[AI Director Online]\x01 %s", msg);
}

public Action Cmd_ReloadPython(int client, int args)
{
	SendSimpleRpcAdminCall(client, "ReloadStrategies", "action", "reload", OnRpcReloadReply);
	ReplyToCommand(client, "[AI Director Online] Requested Python strategy reload...");
	return Plugin_Handled;
}

public void OnRpcReloadReply(JSON result)
{
	char msg[256];
	result.GetString("message", msg, sizeof(msg));
	result.Close();
	PrintToChatAll("\x04[AI Director Online]\x01 %s", msg);
}

public Action Cmd_PingPython(int client, int args)
{
	SendSimpleRpcAdminCall(client, "Ping", "source", "l4d2_llm_director_online", OnRpcPingReply);
	ReplyToCommand(client, "[AI Director Online] Ping sent to Python.");
	return Plugin_Handled;
}

public void OnRpcPingReply(JSON result)
{
	char serverTime[32], strat[64];
	result.GetString("server_time", serverTime, sizeof(serverTime));
	result.GetString("strategy", strat, sizeof(strat));
	result.Close();
	PrintToChatAll("\x04[AI Director Online]\x01 Python Pong (%s) | 策略: \x03%s\x01", serverTime, strat);
}
