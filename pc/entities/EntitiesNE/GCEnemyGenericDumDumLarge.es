/* Next Encounter's DumDumLarge, ported to Serious Engine 1.

   Name and class id are the designers' own (CGCEnemyGenericDumDumLarge, 1120,
   in their Entities.dll, docs/dev-material.md). Model and animations come
   from the disc (tools/creature.py); stats from MinionStats.cfg Object17 over
   Generic, and Extras.DumDumLarge (build/cfg/MinionStats.json).

   Behaviour is cDumDumLargeAI's (vtable 0x801ecbf0, docs/enemies.md,
   "cDumDumLargeAI"), on SE1's CEnemyBase for sight, chase and pain:
   - alerted, it chases its target with a cMoveToObject directive that stops
     10 units away or gives up after MoveTimeOut (state 5, 0x80022040);
   - there it lunges: it heads for a point 2 units past where the target
     stood, along its own heading, at its running speed (state 6,
     0x80021e34 and 0x8008a3f4);
   - 12/25 s into the lunge it arms (the minion's attack sound, flag 0x20);
     armed, a contact closer than 1 unit beyond both collision radii hits the
     target once for Extras Damage and disarms (cDumDumLarge::Collided,
     0x800154c4);
   - the lunge lasts while its directive queue is empty (0x80096ad0): a hit
     reaction, or the stuck recovery after 10 blocked frames, ends it, and it
     chases again. */

1120
%{
#include "EntitiesNE/StdH/StdH.h"
#include "Models/NextEncounter/Enemies/DumDumlarge/DumDumlarge.h"

// MinionStats.cfg, Object17 "DumDumLarge" over Generic
#define DUMDUM_HEALTH           30.0f   // Health
#define DUMDUM_NORMAL_SPEED     10.0f   // NormalSpeed
#define DUMDUM_FAST_SPEED       14.0f   // FastSpeed
#define DUMDUM_SCORE            100     // ScoreNormal
#define DUMDUM_SEE_DIST         500.0f  // Generic SeeDist
#define DUMDUM_HEAR_DIST        50.0f   // Generic HearDist
#define DUMDUM_FOV              90.0f   // Generic FOV
#define DUMDUM_TURN_TIME        1.0f    // Generic TurnSpeed: seconds for a full circle
#define DUMDUM_COLLISION_RADIUS 1.0f    // CollisionRadius
#define DUMDUM_MOVE_TIMEOUT     5.0f    // Generic MoveTimeOut: cMoveToObject gives up
// Extras.DumDumLarge
#define DUMDUM_DAMAGE           5.0f    // Damage
// cDumDumLargeAI and cDumDumLarge constants (main.dol)
#define DUMDUM_CHASE_STOP       10.0f   // cMoveToObject stop distance (0x801ecbe4)
#define DUMDUM_LUNGE_PAST       2.0f    // lunge point past the target (0x801ecbd8)
#define DUMDUM_ARM_TIME         (12.0f/25.0f)  // [r13-0x7ed0] / 25.0 (0x801ecbdc)
#define DUMDUM_HIT_GAP          1.0f    // armed contact reach beyond both radii (0x801e5f80)
#define DUMDUM_STUCK_TIME       (10.0f/60.0f)  // stuck recovery after 10 blocked frames (0x80096424)

static EntityInfo eiDumDumLarge = {
  EIBT_FLESH, 200.0f,
  0.0f, 1.2f, 0.0f,     // source (eyes)
  0.0f, 0.8f, 0.0f,     // target (body)
};
%}

uses "EntitiesMP/EnemyBase";
uses "EntitiesMP/BloodSpray";

class CGCEnemyGenericDumDumLarge : CEnemyBase {
name      "GCEnemyGenericDumDumLarge";
thumbnail "";

properties:
  1 BOOL m_bExploded = FALSE,
  2 BOOL m_bArmed = FALSE,             // flag 0x20: the next close contact hits
  3 FLOAT m_tmLunge = 0.0f,            // time into the lunge (+0x38)
  4 FLOAT3D m_vLungeTarget = FLOAT3D(0,0,0),  // target's position as the lunge began (+0x2c)
  5 BOOL m_bStruck = FALSE,            // armed once this lunge
  6 BOOL m_bBlocked = FALSE,           // blocked this tick
  7 FLOAT m_tmBlocked = 0.0f,          // time blocked in a row

components:
  1 class   CLASS_BASE            "Classes\\EnemyBase.ecl",
  2 class   CLASS_BLOOD_SPRAY     "Classes\\BloodSpray.ecl",
 10 model   MODEL_DUMDUM          "Models\\NextEncounter\\Enemies\\DumDumlarge\\DumDumlarge.mdl",
 11 texture TEXTURE_DUMDUM        "Models\\NextEncounter\\Enemies\\DumDumlarge\\DumDumlarge.tex",

functions:
  virtual CTString GetPlayerKillDescription(const CTString &strPlayerName, const EDeath &eDeath)
  {
    CTString str;
    str.PrintF(TRANSV("%s was flattened by a Dum Dum"), (const char *) strPlayerName);
    return str;
  }

  void *GetEntityInfo(void)
  {
    return &eiDumDumLarge;
  };

  void Precache(void)
  {
    CEnemyBase::Precache();
    PrecacheClass(CLASS_BLOOD_SPRAY);
  };

  // NE's clips: idle1, run1, recoilback1, recoilforward1, laugh1
  void StandingAnim(void)
  {
    StartModelAnim(DUMDUMLARGE_ANIM_IDLE1, AOF_LOOPING|AOF_NORESTART);
  };
  void WalkingAnim(void)
  {
    StartModelAnim(DUMDUMLARGE_ANIM_RUN1, AOF_LOOPING|AOF_NORESTART);
  };
  void RunningAnim(void)
  {
    StartModelAnim(DUMDUMLARGE_ANIM_RUN1, AOF_LOOPING|AOF_NORESTART);
  };
  void RotatingAnim(void)
  {
    StartModelAnim(DUMDUMLARGE_ANIM_RUN1, AOF_LOOPING|AOF_NORESTART);
  };
  INDEX AnimForDamage(FLOAT fDamage)
  {
    // a hit reaction ends a lunge (0x80096ad0)
    m_bArmed = FALSE;
    // hit from the front recoils backward
    FLOAT3D vFront;
    GetHeadingDirection(0, vFront);
    FLOAT fDamageDir = m_vDamage%vFront;
    INDEX iAnim = fDamageDir<0 ? DUMDUMLARGE_ANIM_RECOILBACK1 : DUMDUMLARGE_ANIM_RECOILFORWARD1;
    StartModelAnim(iAnim, 0);
    return iAnim;
  };
  INDEX AnimForDeath(void)
  {
    // NE's DumDums burst (SplatterType 1); there is no death clip
    StartModelAnim(DUMDUMLARGE_ANIM_RECOILBACK1, 0);
    return DUMDUMLARGE_ANIM_RECOILBACK1;
  };

  BOOL ShouldBlowUp(void)
  {
    return TRUE;
  };

  void BlowUpNotify(void)
  {
    if (!m_bExploded) {
      m_bExploded = TRUE;
      CPlacement3D plSpray = GetPlacement();
      CEntity *penSpray = CreateEntity(plSpray, CLASS_BLOOD_SPRAY);
      penSpray->SetParent(this);
      ESpawnSpray eSpawnSpray;
      eSpawnSpray.colBurnColor = C_WHITE|CT_OPAQUE;
      eSpawnSpray.fDamagePower = 2.0f;
      eSpawnSpray.fSizeMultiplier = 1.5f;
      eSpawnSpray.sptType = SPT_BLOOD;
      eSpawnSpray.vDirection = en_vCurrentTranslationAbsolute/8.0f;
      eSpawnSpray.penOwner = this;
      penSpray->Initialize(eSpawnSpray);
    }
  };

  // the gap between two bodies' collision spheres, as cDumDumLarge::Collided measures it
  FLOAT ContactGap(CEntity *pen)
  {
    FLOATaabbox3D box;
    pen->GetBoundingBox(box);
    FLOAT fOtherRadius = Max(box.Size()(1), box.Size()(3))/2.0f;
    FLOAT3D vDelta = pen->GetPlacement().pl_PositionVector-GetPlacement().pl_PositionVector;
    return vDelta.Length()-DUMDUM_COLLISION_RADIUS-fOtherRadius;
  };

procedures:
  // cDumDumLargeAI state 6: the lunge (see the top of the file)
  Hit(EVoid) : CEnemyBase::Hit
  {
    // where the target stood as the lunge began (state 6 entry, +0x2c)
    m_vLungeTarget = m_penEnemy->GetPlacement().pl_PositionVector;
    m_tmLunge = 0.0f;
    m_tmBlocked = 0.0f;
    m_bArmed = FALSE;
    m_bStruck = FALSE;
    RunningAnim();

    while (TRUE)
    {
      // every update, head for 2 units past that spot along our own heading,
      // at running speed (0x8008a3f4 sets the velocity; it has no arrival test)
      FLOAT3D vHeading;
      GetHeadingDirection(0, vHeading);
      m_vDesiredPosition = m_vLungeTarget+vHeading*DUMDUM_LUNGE_PAST;
      m_fMoveSpeed = DUMDUM_FAST_SPEED;
      m_aRotateSpeed = m_aCloseRotateSpeed;
      SetDesiredMovement();

      wait(_pTimer->TickQuantum)
      {
        on (EBegin) : { resume; }
        on (ESound) : { resume; }
        on (EWatch) : { resume; }
        on (EBlock) : { m_bBlocked = TRUE; resume; }
        on (ETimer) : { stop; }
      }

      // 12/25 s in, arm once (0x30c plays the attack sound; +0x38 = -1000)
      m_tmLunge += _pTimer->TickQuantum;
      if (!m_bStruck && m_tmLunge>DUMDUM_ARM_TIME) {
        m_bArmed = TRUE;
        m_bStruck = TRUE;
      }
      // armed, the target within 1 unit beyond both radii takes the hit
      if (m_bArmed && m_penEnemy!=NULL && ContactGap(m_penEnemy)<DUMDUM_HIT_GAP) {
        FLOAT3D vDirection = m_penEnemy->GetPlacement().pl_PositionVector-GetPlacement().pl_PositionVector;
        vDirection.SafeNormalize();
        InflictDirectDamage(m_penEnemy, this, DMT_CLOSERANGE, DUMDUM_DAMAGE,
          m_penEnemy->GetPlacement().pl_PositionVector, vDirection);
        m_bArmed = FALSE;
      }
      // blocked for more than 10 frames, NE's stuck recovery (0x80096424)
      // queues a directive and the lunge gives way to the chase
      if (m_bBlocked) {
        m_tmBlocked += _pTimer->TickQuantum;
        m_bBlocked = FALSE;
      } else {
        m_tmBlocked = 0.0f;
      }
      // the port's own limit: NE's exit from a lunge that misses and keeps
      // circling the spot is not decoded, so it gives up after MoveTimeOut
      if (m_tmBlocked>DUMDUM_STUCK_TIME || m_tmLunge>DUMDUM_MOVE_TIMEOUT || m_penEnemy==NULL) {
        m_bArmed = FALSE;
        return EReturn();
      }
    }
  };

  Main(EVoid)
  {
    InitAsModel();
    SetPhysicsFlags(EPF_MODEL_WALKING|EPF_HASLUNGS);
    SetCollisionFlags(ECF_MODEL);
    SetFlags(GetFlags()|ENF_ALIVE);
    SetHealth(DUMDUM_HEALTH);
    m_fMaxHealth = DUMDUM_HEALTH;
    en_fDensity = 1000.0f;

    SetModel(MODEL_DUMDUM);
    SetModelMainTexture(TEXTURE_DUMDUM);

    // movement: NE's NormalSpeed walks, FastSpeed chases; a full turn takes TurnSpeed seconds
    m_fWalkSpeed = DUMDUM_NORMAL_SPEED;
    m_aWalkRotateSpeed = AngleDeg(360.0f/DUMDUM_TURN_TIME);
    m_fAttackRunSpeed = DUMDUM_FAST_SPEED;
    m_aAttackRotateSpeed = AngleDeg(360.0f/DUMDUM_TURN_TIME);
    m_fCloseRunSpeed = DUMDUM_FAST_SPEED;
    m_aCloseRotateSpeed = AngleDeg(360.0f/DUMDUM_TURN_TIME);
    // sight, and the chase that stops 10 units away and lunges from there
    m_fViewAngle = DUMDUM_FOV;
    m_fSenseRange = DUMDUM_HEAR_DIST;
    m_fAttackDistance = DUMDUM_SEE_DIST;
    m_fCloseDistance = DUMDUM_CHASE_STOP;
    m_fStopDistance = 0.0f;
    m_fAttackFireTime = 0.5f;
    m_fCloseFireTime = 0.1f;
    m_fIgnoreRange = DUMDUM_SEE_DIST;
    // damage and death
    m_fBlowUpAmount = 0.0f;
    m_fBodyParts = 0;
    m_fDamageWounded = 10.0f;
    m_iScore = DUMDUM_SCORE;
    m_sptType = SPT_BLOOD;

    StandingAnim();
    jump CEnemyBase::MainLoop();
  };
};
