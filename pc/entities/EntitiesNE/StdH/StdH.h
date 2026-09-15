/* Next Encounter's entity classes (pc/entities), built against the engine
   fork in pc/engine the way its own EntitiesMP is.

   The classes derive from EntitiesMP's (CEnemyBase and the rest), which live
   in EntitiesMP.dll: their headers are read with DECL_DLL as an import, and
   DECL_DLL becomes an export again for the classes this DLL defines. */

#include <Engine/Engine.h>
#include <GameMP/SessionProperties.h>
#include <GameMP/PlayerSettings.h>

#ifdef _MSC_VER
#ifndef _offsetof
#define _offsetof offsetof
#endif
#endif

#define DECL_DLL _declspec(dllimport)
#include <EntitiesMP/Global.h>
#include <EntitiesMP/Common/Flags.h>
#include <EntitiesMP/Common/Common.h>
#include <EntitiesMP/Common/Particles.h>
#include <EntitiesMP/Common/EmanatingParticles.h>
#include <EntitiesMP/Common/GameInterface.h>
#include <EntitiesMP/EnemyBase.h>
#include <EntitiesMP/Player.h>
#include <EntitiesMP/BasicEffects.h>
#include <EntitiesMP/BloodSpray.h>
#undef DECL_DLL

#define DECL_DLL _declspec(dllexport)
