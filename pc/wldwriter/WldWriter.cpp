// WldWriter -- Stage 2 of docs/world-conversion.md: build a Serious Engine 1
// .wld from a level description.
//
// For now a smoke test of the chain the writer needs: start the engine, load
// entity classes (retail Classes\*.ecl, resolved to our EntitiesMP.dll through
// ModEXT.txt), build a WorldBase brush from a CObject3D, save the world and
// load it back.
//
// It must run from pc/engine/SamTSE/Bin: the engine takes the parent of the
// exe's folder as its game root. See pc/README.md.

#include <Engine/Engine.h>
#include <Engine/Brushes/Brush.h>
#include <Engine/Math/Object3D.h>
#include <Engine/World/World.h>
#include <Engine/World/WorldRayCasting.h>
#include <Engine/Templates/Stock_CEntityClass.h>
#include <Engine/Graphics/Texture.h>
#include <Engine/Math/TextureMapping.h>
#include <Engine/Graphics/Fog.h>
#include <Engine/Models/EditModel.h>
#include <Engine/Models/ModelObject.h>
#include <Engine/Models/ModelData.h>
#include <Engine/Models/Model_internal.h>
#include <Engine/Templates/DynamicArray.cpp>
#include <Engine/Templates/DynamicContainer.cpp>
#include <Engine/Templates/StaticArray.cpp>
#include <direct.h>
#include <ctype.h>
#include <time.h>
#include <string.h>
#include <stdlib.h>
#include <math.h>
#include <vector>
#include <stdio.h>

// Needed by Engine/Base/ErrorReporting, as in MakeFONT.
HWND _hwndMain = NULL;

// One closed box as six quads.
static void AddBox(CObjectSector &osc, CObjectMaterial &mat, const DOUBLE3D &lo, const DOUBLE3D &hi)
{
  DOUBLE3D v[8];
  for (INDEX i = 0; i < 8; i++) {
    v[i] = DOUBLE3D((i & 1) ? hi(1) : lo(1), (i & 2) ? hi(2) : lo(2), (i & 4) ? hi(3) : lo(3));
  }
  static const INDEX aiFace[6][4] = {
    {0, 2, 3, 1}, {4, 5, 7, 6}, {0, 1, 5, 4}, {2, 6, 7, 3}, {0, 4, 6, 2}, {1, 3, 7, 5}};
  for (INDEX f = 0; f < 6; f++) {
    DOUBLE3D q[4] = {v[aiFace[f][0]], v[aiFace[f][1]], v[aiFace[f][2]], v[aiFace[f][3]]};
    osc.CreatePolygon(4, q, mat, 0, FALSE);
  }
}

static void CountBrushes(CWorld &wo, INDEX &ctSectors, INDEX &ctPolygons)
{
  ctSectors = ctPolygons = 0;
  FOREACHINDYNAMICCONTAINER(wo.wo_cenEntities, CEntity, iten) {
    if (iten->en_RenderType != CEntity::RT_BRUSH && iten->en_RenderType != CEntity::RT_FIELDBRUSH) {
      continue;
    }
    CBrushMip *pbm = iten->en_pbrBrush->GetFirstMip();
    if (pbm == NULL) { continue; }                 // a brush with no mip: empty
    FOREACHINDYNAMICARRAY(pbm->bm_abscSectors, CBrushSector, itbsc) {
      ctSectors++;
      ctPolygons += itbsc->bsc_abpoPolygons.Count();
    }
  }
}

#define STEP(msg) printf("step: %s\n", msg)

static int SmokeTest(void)
{
  const CTFileName fnmWorld = CTFILENAME("Levels\\NextEncounter\\Smoke.wld");
  _mkdir(CTString(_fnmApplicationPath + "Levels"));
  _mkdir(CTString(_fnmApplicationPath + "Levels\\NextEncounter"));

  {
    STEP("create world");
    CWorld wo;
    CPlacement3D plOrigin(FLOAT3D(0, 0, 0), ANGLE3D(0, 0, 0));
    STEP("obtain WorldBase class");
    CEntityClass *pec = _pEntityClassStock->Obtain_t(CTFILENAME("Classes\\WorldBase.ecl"));
    printf("  class dll: %s\n", (const char *)pec->ec_fnmClassDLL);
    STEP("create WorldBase entity");
    CEntity *penBase = wo.CreateEntity(plOrigin, pec);
    STEP("initialize");
    penBase->Initialize();

    STEP("build object3d");
    CObject3D o3d;
    CObjectSector &osc = *o3d.ob_aoscSectors.New(1);
    osc.osc_strName = "smoke";
    osc.osc_colAmbient = C_GRAY;
    osc.osc_ulFlags[0] = osc.osc_ulFlags[1] = osc.osc_ulFlags[2] = 0;
    CObjectMaterial &mat = *osc.osc_aomtMaterials.New(1);
    mat = CObjectMaterial(CTString("Textures\\Editor\\Default.tex"));
    AddBox(osc, mat, DOUBLE3D(-8, 0, -8), DOUBLE3D(8, 6, 8));

    STEP("get brush");
    CBrush3D *pbr = penBase->GetBrush();
    if (pbr == NULL) { printf("WorldBase has no brush\n"); return 3; }
    pbr->FromObject3D_t(o3d);
    pbr->CalculateBoundingBoxes();

    STEP("save");
    wo.SetName("WldWriter smoke test");
    wo.Save_t(fnmWorld);
    INDEX ctSec, ctPol;
    CountBrushes(wo, ctSec, ctPol);
    printf("wrote %s: %d entities, %d sectors, %d polygons\n", (const char *)fnmWorld,
           wo.wo_cenEntities.Count(), ctSec, ctPol);
  }
  {
    STEP("load back");
    CWorld wo;
    wo.Load_t(fnmWorld);
    INDEX ctSec, ctPol;
    CountBrushes(wo, ctSec, ctPol);
    printf("read  %s: %d entities, %d sectors, %d polygons, name '%s'\n", (const char *)fnmWorld,
           wo.wo_cenEntities.Count(), ctSec, ctPol, (const char *)wo.GetName());
    return (wo.wo_cenEntities.Count() == 1 && ctSec == 1 && ctPol == 6) ? 0 : 2;
  }
}

// ---- building a world from a .wldsrc file (tools/wldprep.py) --------------

#define NLCH ((char)10)                 // no backslash escapes in this file
static const char SEP[2] = { (char)92, 0 };

struct SPendingLink {              // entity-pointer property, resolved at the end
  CEntity *pl_pen;
  CTString pl_strName;
  INDEX pl_iTarget;
};

static BOOL ReadToken(FILE *f, char *str, int ctMax)
{
  int c;
  for (;;) {                                   // skip space and # comments
    c = fgetc(f);
    if (c == EOF) { return FALSE; }
    if (c == 35) {                             // #
      while (c != EOF && c != 10) { c = fgetc(f); }
      continue;
    }
    if (isspace(c)) { continue; }
    break;
  }
  int i = 0;
  do {
    if (i < ctMax - 1) { str[i++] = (char)c; }
    c = fgetc(f);
  } while (c != EOF && !isspace(c));
  str[i] = 0;
  return TRUE;
}

static CEntityProperty *FindProp(CEntity *pen, const char *strName)
{
  CEntityProperty *pep = pen->PropertyForName(CTString(strName));
  if (pep == NULL) {
    printf("  warning: class has no property [%s]%c", strName, NLCH);
  }
  return pep;
}

// Rest of the line, trimmed: property names contain spaces, as in "Wait time".
static void ReadRestOfLine(FILE *f, char *str, int ctMax)
{
  int c = fgetc(f);
  while (c == 32 || c == 9) { c = fgetc(f); }
  int i = 0;
  while (c != EOF && c != 10 && c != 13) {
    if (i < ctMax - 1) { str[i++] = (char)c; }
    c = fgetc(f);
  }
  while (i > 0 && (str[i-1] == 32 || str[i-1] == 9)) { i--; }
  str[i] = 0;
}

// A texture layer (1 or 2) of a polygon, from a layer line.
struct SPendingLayer {
  BOOL pl_bSet;
  UBYTE pl_ubScroll, pl_ubBlend, pl_ubFlags;         // SE1's, into bpt_auProperties
  COLOR pl_colColor;
  DOUBLE3D pl_vA, pl_vB;                            // U = A.p + A0, V = B.p + B0, in mex
  DOUBLE pl_fA0, pl_fB0;
  DOUBLE pl_fMexW, pl_fMexH;
};

// A polygon from the source, held until its hole and split lines are read.
struct SPendingPolygon {
  CObjectSector *pp_posc;
  INDEX pp_iMaterial;
  BOOL pp_bValid;
  std::vector<std::vector<DOUBLE3D> > pp_aLoops;    // outline, then holes
  std::vector<std::vector<DOUBLE3D> > pp_aSplit;    // convex pieces, if offered
  BOOL pp_bMapped;                                  // a uv line was read
  ULONG pp_ulFlags;                                 // polygon flags: OPOF_PORTAL, BPOF_INVISIBLE, ...
  INDEX pp_iWorldSector;                            // world base sector, or -1 in an entity's brush
  BOOL pp_bSplit;                                   // put in as its convex pieces
  DOUBLE3D pp_vA, pp_vB;                            // U = A.p + A0, V = B.p + B0, in mex
  DOUBLE pp_fA0, pp_fB0;
  DOUBLE pp_fMexW, pp_fMexH;                        // the texture's size in mex
  SPendingLayer pp_aLayers[3];                      // [1] and [2]: texture layers
};

// A mat line's rest: the texture, then optionally layer 1's and layer 2's, "-" for none.
static CObjectMaterial MakeMaterial(const CTString &strLine)
{
  char str[800];
  strncpy(str, (const char *)strLine, sizeof(str) - 1);
  str[sizeof(str) - 1] = 0;
  CTString astr[3];
  INDEX ct = 0;
  for (char *pch = strtok(str, " \t"); pch != NULL && ct < 3; pch = strtok(NULL, " \t")) {
    if (strcmp(pch, "-") != 0) { astr[ct] = pch; }
    ct++;
  }
  CObjectMaterial omt(astr[0]);
  omt.omt_strName2 = astr[1];
  omt.omt_strName3 = astr[2];
  omt.omt_Color = C_WHITE | CT_OPAQUE;
  return omt;
}

// A texture map U = A.p + A0, V = B.p + B0 (mex) as SE1's mapping on the plane.
static void MapToPlane(CMappingDefinition &md, const CObjectPlane &pl, const DOUBLE3D &vA, DOUBLE fA0,
  const DOUBLE3D &vB, DOUBLE fB0, DOUBLE fMexW, DOUBLE fMexH, BOOL bClampU, BOOL bClampV)
{
  CMappingVectors mv;
  mv.FromPlane_DOUBLE(pl);
  const DOUBLE3D vO = FLOATtoDOUBLE(mv.mv_vO);
  const DOUBLE3D vU = FLOATtoDOUBLE(mv.mv_vU);
  const DOUBLE3D vV = FLOATtoDOUBLE(mv.mv_vV);
  md.md_fUoS = (FLOAT)((vA % vU) / 1024.0);
  md.md_fUoT = (FLOAT)((vA % vV) / 1024.0);
  md.md_fVoS = (FLOAT)((vB % vU) / 1024.0);
  md.md_fVoT = (FLOAT)((vB % vV) / 1024.0);
  // a repeating texture's offset is kept within one tile; a clamped one's is where it is
  const DOUBLE fTileU = fMexW / 1024.0, fTileV = fMexH / 1024.0;
  DOUBLE fOffU = ((vA % vO) + fA0) / 1024.0;
  DOUBLE fOffV = ((vB % vO) + fB0) / 1024.0;
  if (!bClampU) {
    fOffU = fmod(fOffU, fTileU);
    if (fOffU < 0) { fOffU += fTileU; }
  }
  if (!bClampV) {
    fOffV = fmod(fOffV, fTileV);
    if (fOffV < 0) { fOffV += fTileV; }
  }
  md.md_fUOffset = (FLOAT)-fOffU;
  md.md_fVOffset = (FLOAT)-fOffV;
}

// A polygon's texture map, turned into SE1's mapping for the plane the
// polygon actually got. SE1 draws a point's texture coordinate as
// ((s*UoS + t*UoT) - UOffset) * 1024 mex, s and t being the point along the
// plane's default mapping axes from its reference point: the renderer moves
// the mapping origin by the offset (CMappingDefinition::MakeMappingVectors,
// used by RenCache.cpp), so the offset is subtracted. The editor-side
// CMappingDefinition::GetTextureCoordinates adds it instead; it is not what
// is drawn. A point on the plane is O + s*U + t*V, so
// A.p + A0 = (A.O + A0) + s*(A.U) + t*(A.V).
//
// Texture layers 1 and 2 get their own mapping the same way. Their scroll,
// blend and flags go where brush import reads a polygon's texture properties
// (opo_ubUserData: CBrushPolygonProperties, three bpt_auProperties, the
// shadow colour), which it takes only from a polygon marked
// BPOF_WASBRUSHPOLYGON, so everything there is set as import would set a new
// polygon's, then the layers.
static void SetMapping(CObjectPolygon &opo, const SPendingPolygon &pp)
{
  if (opo.opo_Plane == NULL) { return; }
  if (pp.pp_bMapped) {
    const UBYTE ubBaseFlags = pp.pp_aLayers[0].pl_bSet ? pp.pp_aLayers[0].pl_ubFlags : 0;
    MapToPlane(opo.opo_amdMappings[0], *opo.opo_Plane, pp.pp_vA, pp.pp_fA0, pp.pp_vB, pp.pp_fB0,
               pp.pp_fMexW, pp.pp_fMexH, (ubBaseFlags & BPTF_CLAMPU) != 0, (ubBaseFlags & BPTF_CLAMPV) != 0);
  }
  if (!pp.pp_aLayers[0].pl_bSet && !pp.pp_aLayers[1].pl_bSet && !pp.pp_aLayers[2].pl_bSet) { return; }
  const size_t ctPolygonProperties = sizeof(CBrushPolygonProperties), ctTextureProperties = 8;
  ASSERT(sizeof(opo.opo_ubUserData) >= ctPolygonProperties + 3 * ctTextureProperties + sizeof(COLOR));
  UBYTE *pub = opo.opo_ubUserData;
  memset(pub, 0, sizeof(opo.opo_ubUserData));
  CBrushPolygonProperties bpp;
  bpp.bpp_ubShadowBlend = BPT_BLEND_SHADE;
  memcpy(pub, &bpp, ctPolygonProperties);
  const COLOR colWhite = C_WHITE | CT_OPAQUE;
  for (INDEX iLayer = 0; iLayer < 3; iLayer++) {
    UBYTE *pubTexture = pub + ctPolygonProperties + iLayer * ctTextureProperties;
    const SPendingLayer &pl = pp.pp_aLayers[iLayer];
    BOOL bSet = pl.pl_bSet;
    pubTexture[0] = bSet ? pl.pl_ubScroll : 0;                                              // bpt_ubScroll
    pubTexture[1] = bSet ? pl.pl_ubBlend : (iLayer == 0 ? BPT_BLEND_OPAQUE : BPT_BLEND_SHADE); // bpt_ubBlend
    pubTexture[2] = bSet ? pl.pl_ubFlags : BPTF_DISCARDABLE;                                // bpt_ubFlags
    memcpy(pubTexture + 4, bSet ? &pl.pl_colColor : &colWhite, sizeof(COLOR));              // bpt_colColor
    if (bSet && iLayer > 0) {
      MapToPlane(opo.opo_amdMappings[iLayer], *opo.opo_Plane, pl.pl_vA, pl.pl_fA0, pl.pl_vB, pl.pl_fB0,
                 pl.pl_fMexW, pl.pl_fMexH, (pl.pl_ubFlags & BPTF_CLAMPU) != 0, (pl.pl_ubFlags & BPTF_CLAMPV) != 0);
    }
  }
  memcpy(pub + ctPolygonProperties + 3 * ctTextureProperties, &colWhite, sizeof(COLOR));   // bpo_colShadow
  opo.opo_ulFlags |= BPOF_WASBRUSHPOLYGON;
}

// Does SE1's triangulator accept this polygon? Built alone in the brush of a
// scratch entity: brush import reaches for its entity and world.
static BOOL PolygonTriangulates(const SPendingPolygon &pp, const CTString &strMaterial, CEntity *penScratch)
{
  CObject3D o3d;
  CObjectSector *posc = o3d.ob_aoscSectors.New(1);
  posc->osc_colAmbient = C_GRAY;
  posc->osc_ulFlags[0] = posc->osc_ulFlags[1] = posc->osc_ulFlags[2] = 0;
  *posc->osc_aomtMaterials.New(1) = CObjectMaterial(strMaterial);
  std::vector<DOUBLE3D> av = pp.pp_aLoops[0];
  CObjectPolygon *popo = posc->CreatePolygon((INDEX)av.size(), &av[0], posc->osc_aomtMaterials[0], 0, FALSE);
  if (popo == NULL) { return FALSE; }
  for (size_t iLoop = 1; iLoop < pp.pp_aLoops.size(); iLoop++) {
    const std::vector<DOUBLE3D> &aHole = pp.pp_aLoops[iLoop];
    for (size_t i = 0; i < aHole.size(); i++) {
      posc->CreateEdgeInPolygon(*popo, aHole[i], aHole[(i + 1) % aHole.size()]);
    }
  }
  CBrush3D *pbr = penScratch->GetBrush();
  pbr->FromObject3D_t(o3d);

  CBrushMip *pbm = pbr->GetFirstMip();
  if (pbm == NULL) { return FALSE; }
  BOOL bTriangulates = TRUE;
  FOREACHINDYNAMICARRAY(pbm->bm_abscSectors, CBrushSector, itbsc) {
    FOREACHINSTATICARRAY(itbsc->bsc_abpoPolygons, CBrushPolygon, itbpo) {
      itbpo->Triangulate();
      if (itbpo->bpo_ulFlags & BPOF_INVALIDTRIANGLES) { bTriangulates = FALSE; }
    }
  }
  return bTriangulates;
}

// A sector of the zoning world base, kept until every polygon is read.
struct SWorldSector {
  CTString ws_strName;
  COLOR ws_colAmbient;
  ULONG ws_ulContent;
  std::vector<CTString> ws_astrMaterials;
  std::vector<INDEX> ws_aiPolygons;                 // into the world's pending polygons
};

// Put a pending polygon into its sector: whole with its holes, or, when it
// offers convex pieces and SE1 cannot triangulate it whole, as those pieces.
static void FlushPolygon(SPendingPolygon &pp, CEntity *penScratch, INDEX &ctPolygons, INDEX &ctHoles, INDEX &ctSplit,
  std::vector<SWorldSector> &aWorldSectors, std::vector<SPendingPolygon> &aWorldPolygons)
{
  if (pp.pp_iWorldSector >= 0) {
    // a world base polygon: kept, and built once every sector is read
    INDEX iSector = pp.pp_iWorldSector;
    pp.pp_iWorldSector = -1;
    if (!pp.pp_bValid || pp.pp_aLoops.empty() || pp.pp_aLoops[0].size() < 3) { return; }
    aWorldSectors[iSector].ws_aiPolygons.push_back((INDEX)aWorldPolygons.size());
    aWorldPolygons.push_back(pp);
    return;
  }
  if (pp.pp_posc == NULL) { return; }
  CObjectSector *posc = pp.pp_posc;
  pp.pp_posc = NULL;
  if (!pp.pp_bValid || pp.pp_aLoops.empty() || pp.pp_aLoops[0].size() < 3) { return; }
  CObjectMaterial &omt = posc->osc_aomtMaterials[pp.pp_iMaterial];
  if (!pp.pp_aSplit.empty() && !PolygonTriangulates(pp, omt.omt_Name, penScratch)) {
    ctSplit++;
    for (size_t iPiece = 0; iPiece < pp.pp_aSplit.size(); iPiece++) {
      std::vector<DOUBLE3D> &av = pp.pp_aSplit[iPiece];
      CObjectPolygon *popoPiece = av.size() >= 3
        ? posc->CreatePolygon((INDEX)av.size(), &av[0], omt, pp.pp_ulFlags, FALSE) : NULL;
      if (popoPiece != NULL) {
        SetMapping(*popoPiece, pp);
        ctPolygons++;
      }
    }
    return;
  }
  // The vertex-array variant. The index variant builds each edge from the
  // next vertex to the current one, so its edges do not chain and every
  // polygon fails triangulation (BPOF_INVALIDTRIANGLES) in the editor.
  // FromObject3D_t merges the duplicate vertices this creates.
  std::vector<DOUBLE3D> &av = pp.pp_aLoops[0];
  CObjectPolygon *popo = posc->CreatePolygon((INDEX)av.size(), &av[0], omt, pp.pp_ulFlags, FALSE);
  if (popo == NULL) { return; }
  SetMapping(*popo, pp);
  ctPolygons++;
  // Holes: their edges join the polygon's, wound the other way. A Serious
  // Engine polygon is a set of edges, not a single loop.
  for (size_t iLoop = 1; iLoop < pp.pp_aLoops.size(); iLoop++) {
    const std::vector<DOUBLE3D> &aHole = pp.pp_aLoops[iLoop];
    for (size_t i = 0; i < aHole.size(); i++) {
      posc->CreateEdgeInPolygon(*popo, aHole[i], aHole[(i + 1) % aHole.size()]);
    }
    ctHoles++;
  }
}

// The world base's sectors as an object. With bTag, each polygon's colour is
// its index + 1, so that a brush polygon can be traced back to its source.
static void BuildWorldObject(CObject3D &o3d, std::vector<SWorldSector> &aSectors,
  std::vector<SPendingPolygon> &aPolygons, BOOL bTag, INDEX &ctPolygons, INDEX &ctHoles, INDEX &ctSplit)
{
  o3d.Clear();
  ctPolygons = ctHoles = ctSplit = 0;
  for (size_t iSector = 0; iSector < aSectors.size(); iSector++) {
    SWorldSector &ws = aSectors[iSector];
    CObjectSector *posc = o3d.ob_aoscSectors.New(1);
    posc->osc_strName = ws.ws_strName;
    posc->osc_colAmbient = ws.ws_colAmbient;
    posc->osc_ulFlags[0] = ws.ws_ulContent << BSCB_CONTENTTYPE;
    posc->osc_ulFlags[1] = posc->osc_ulFlags[2] = 0;
    for (size_t iMat = 0; iMat < ws.ws_astrMaterials.size(); iMat++) {
      *posc->osc_aomtMaterials.New(1) = MakeMaterial(ws.ws_astrMaterials[iMat]);
    }
    for (size_t i = 0; i < ws.ws_aiPolygons.size(); i++) {
      INDEX iPolygon = ws.ws_aiPolygons[i];
      SPendingPolygon &pp = aPolygons[iPolygon];
      CObjectMaterial &omt = posc->osc_aomtMaterials[pp.pp_iMaterial];
      if (pp.pp_bSplit) {
        ctSplit++;
        for (size_t iPiece = 0; iPiece < pp.pp_aSplit.size(); iPiece++) {
          std::vector<DOUBLE3D> &av = pp.pp_aSplit[iPiece];
          if (av.size() < 3) { continue; }
          CObjectPolygon *popo = posc->CreatePolygon((INDEX)av.size(), &av[0], omt, pp.pp_ulFlags, FALSE);
          if (popo == NULL) { continue; }
          SetMapping(*popo, pp);
          if (bTag) { popo->opo_colorColor = (COLOR)(iPolygon + 1); }
          ctPolygons++;
        }
        continue;
      }
      std::vector<DOUBLE3D> &av = pp.pp_aLoops[0];
      CObjectPolygon *popo = posc->CreatePolygon((INDEX)av.size(), &av[0], omt, pp.pp_ulFlags, FALSE);
      if (popo == NULL) { continue; }
      SetMapping(*popo, pp);
      if (bTag) { popo->opo_colorColor = (COLOR)(iPolygon + 1); }
      ctPolygons++;
      for (size_t iLoop = 1; iLoop < pp.pp_aLoops.size(); iLoop++) {
        const std::vector<DOUBLE3D> &aHole = pp.pp_aLoops[iLoop];
        for (size_t k = 0; k < aHole.size(); k++) {
          posc->CreateEdgeInPolygon(*popo, aHole[k], aHole[(k + 1) % aHole.size()]);
        }
        ctHoles++;
      }
    }
  }
}

// SE1's triangulator judges a polygon in its sector: the import splits its
// edges at every collinear vertex of the sector (CObjectSector::Optimize,
// SplitCollinearEdges), so a polygon that triangulates alone can fail beside
// its neighbours. Build the world base in a scratch brush, triangulate every
// polygon there, and put the ones that fail in as their convex pieces; repeat
// while that changes anything.
static void SplitWhereInvalid(std::vector<SWorldSector> &aSectors, std::vector<SPendingPolygon> &aPolygons,
  CEntity *penScratch, INDEX &ctPasses)
{
  ctPasses = 0;
  for (INDEX iPass = 0; iPass < 4; iPass++) {
    CObject3D o3d;
    INDEX ctPolygons, ctHoles, ctSplit;
    BuildWorldObject(o3d, aSectors, aPolygons, TRUE, ctPolygons, ctHoles, ctSplit);
    CBrush3D *pbr = penScratch->GetBrush();
    pbr->FromObject3D_t(o3d);
    CBrushMip *pbm = pbr->GetFirstMip();
    if (pbm == NULL) { return; }
    INDEX ctChanged = 0, ctInvalid = 0, ctInvalidPieces = 0, ctInvalidWhole = 0, ctInvalidClosing = 0;
    ctPasses++;
    FOREACHINDYNAMICARRAY(pbm->bm_abscSectors, CBrushSector, itbsc) {
      FOREACHINSTATICARRAY(itbsc->bsc_abpoPolygons, CBrushPolygon, itbpo) {
        itbpo->bpo_apbvxTriangleVertices.Clear();
        itbpo->Triangulate();
        if (!(itbpo->bpo_ulFlags & BPOF_INVALIDTRIANGLES)) { continue; }
        INDEX iPolygon = (INDEX)itbpo->bpo_colColor - 1;
        if (iPolygon < 0 || iPolygon >= (INDEX)aPolygons.size()) { continue; }
        SPendingPolygon &pp = aPolygons[iPolygon];
        ctInvalid++;
        if (pp.pp_bSplit) { ctInvalidPieces++; }
        else if (pp.pp_aSplit.empty()) { ctInvalidWhole++; }
        if (pp.pp_ulFlags & (OPOF_PORTAL | BPOF_INVISIBLE)) { ctInvalidClosing++; }
        if (!pp.pp_bSplit && !pp.pp_aSplit.empty()) {
          pp.pp_bSplit = TRUE;
          ctChanged++;
        }
      }
    }
    printf("  triangulation pass %d: %d invalid (%d portal or closing), %d split now, %d already pieces, %d with no pieces%c",
           iPass, ctInvalid, ctInvalidClosing, ctChanged, ctInvalidPieces, ctInvalidWhole, NLCH);
    if (ctChanged == 0) { return; }
  }
}

// n vertex indices into the sector's pool -> positions; FALSE on a bad index
static BOOL ReadLoop(FILE *f, const std::vector<DOUBLE3D> &aPool, std::vector<DOUBLE3D> &av)
{
  INDEX ct = 0;
  fscanf(f, "%d", &ct);
  BOOL bValid = ct >= 3;
  av.clear();
  for (INDEX i = 0; i < ct; i++) {
    INDEX iv = 0;
    fscanf(f, "%d", &iv);
    if (iv < 0 || iv >= (INDEX)aPool.size()) { bValid = FALSE; continue; }
    av.push_back(aPool[iv]);
  }
  return bValid;
}

static int BuildFromSource(const char *strSource)
{
  FILE *f = fopen(strSource, "rt");
  if (f == NULL) { printf("cannot open %s%c", strSource, NLCH); return 1; }

  char str[512];
  char strWorld[256] = "world";
  CWorld wo;
  const CPlacement3D plOrigin(FLOAT3D(0, 0, 0), ANGLE3D(0, 0, 0));

  CObject3D o3dWorld;               // the static world, one WorldBase
  CObject3D o3dEntity;              // brush of the entity being read
  CObjectSector *posc = NULL;       // sector that verts/mat/poly lines go to
  CEntity *penCurrent = NULL;       // entity being read, NULL outside a block
  BOOL bEntityHasBrush = FALSE;

  std::vector<CEntity *> aEntities;
  std::vector<SPendingLink> aLinks;
  std::vector<DOUBLE3D> aPool;       // vertices of the current sector, by index
  INDEX ctPolygons = 0, ctVertices = 0, ctSectors = 0, ctProps = 0, ctEmptyObjects = 0;
  INDEX ctHoles = 0, ctSplit = 0;
  std::vector<SWorldSector> aWorldSectors;        // the world base, built at the end
  std::vector<SPendingPolygon> aWorldPolygons;
  INDEX iWorldSector = -1;          // world sector that verts/mat/poly lines go to
  ULONG ulPolygonFlags = 0;         // polyflags: added to every polygon's flags
  COLOR colBrushAmbient = C_GRAY;   // ambient: of an entity brush's implicit sector
  SPendingPolygon pending;          // a poly line waits for its hole and split lines
  pending.pp_posc = NULL;
  pending.pp_iWorldSector = -1;
  pending.pp_bSplit = FALSE;
  pending.pp_bMapped = FALSE;
  pending.pp_ulFlags = 0;
  for (INDEX iLayer = 0; iLayer < 3; iLayer++) { pending.pp_aLayers[iLayer].pl_bSet = FALSE; }
  CWorld woScratch;                 // where polygons are test-triangulated
  CEntity *penScratch = woScratch.CreateEntity_t(plOrigin,
    CTFileName(CTString("Classes") + SEP + "WorldBase.ecl"));
  penScratch->Initialize();

  while (ReadToken(f, str, sizeof(str))) {
    if (strcmp(str, "hole") != 0 && strcmp(str, "split") != 0 && strcmp(str, "uv") != 0
        && strcmp(str, "flags") != 0 && strcmp(str, "layer") != 0) {
      FlushPolygon(pending, penScratch, ctPolygons, ctHoles, ctSplit, aWorldSectors, aWorldPolygons);
    }

    if (strcmp(str, "world") == 0) {
      ReadToken(f, strWorld, sizeof(strWorld));

    } else if (strcmp(str, "polyflags") == 0) {
      unsigned long ul = 0;
      fscanf(f, "%lx", &ul);
      ulPolygonFlags = (ULONG)ul;

    } else if (strcmp(str, "ambient") == 0) {
      unsigned long ul = 0;
      fscanf(f, "%lx", &ul);
      colBrushAmbient = (COLOR)ul;

    } else if (strcmp(str, "sector") == 0) {
      ULONG ulContent = 0, ulAmbient = 0;
      char strName[128] = "sector";
      fscanf(f, "%u %x", &ulContent, &ulAmbient);
      ReadToken(f, strName, sizeof(strName));
      ctSectors++;
      // inside an entity block: a sector of that entity's brush
      if (penCurrent != NULL) {
        posc = o3dEntity.ob_aoscSectors.New(1);
        bEntityHasBrush = TRUE;
        posc->osc_strName = strName;
        posc->osc_colAmbient = (COLOR)ulAmbient;
        posc->osc_ulFlags[0] = ulContent << BSCB_CONTENTTYPE;
        posc->osc_ulFlags[1] = posc->osc_ulFlags[2] = 0;
      } else {
        SWorldSector ws;
        ws.ws_strName = strName;
        ws.ws_colAmbient = (COLOR)ulAmbient;
        ws.ws_ulContent = ulContent;
        aWorldSectors.push_back(ws);
        iWorldSector = (INDEX)aWorldSectors.size() - 1;
        posc = NULL;
      }

    } else if (strcmp(str, "sectorflags") == 0) {
      // sectorflags <haze> <environment>: of the entity sector just above
      unsigned int uHaze = 0, uEnvironment = 0;
      fscanf(f, "%u %u", &uHaze, &uEnvironment);
      if (posc != NULL) {
        posc->osc_ulFlags[0] |= (uHaze & 0xF) << BSCB_HAZETYPE;
        posc->osc_ulFlags[1] |= (uEnvironment & 0xFF) << BSCB2_ENVIRONMENTTYPE;
      }

    } else if (strcmp(str, "entity") == 0) {
      char strClass[128] = "";
      double p[3], m[9];
      ReadToken(f, strClass, sizeof(strClass));
      fscanf(f, "%lf %lf %lf", &p[0], &p[1], &p[2]);
      for (INDEX i = 0; i < 9; i++) { fscanf(f, "%lf", &m[i]); }
      FLOATmatrix3D mRot;
      for (INDEX r = 0; r < 3; r++) {
        for (INDEX c = 0; c < 3; c++) { mRot(r + 1, c + 1) = (FLOAT)m[r * 3 + c]; }
      }
      CPlacement3D pl;
      pl.pl_PositionVector = FLOAT3D((FLOAT)p[0], (FLOAT)p[1], (FLOAT)p[2]);
      DecomposeRotationMatrixNoSnap(pl.pl_OrientationAngle, mRot);
      CTFileName fnmClass = CTFileName(CTString("Classes") + SEP + strClass + ".ecl");
      penCurrent = wo.CreateEntity_t(pl, fnmClass);
      aEntities.push_back(penCurrent);
      o3dEntity.Clear();
      bEntityHasBrush = FALSE;
      posc = NULL;
      iWorldSector = -1;

    } else if (strcmp(str, "endentity") == 0 && penCurrent != NULL) {
      penCurrent->Initialize();
      if (bEntityHasBrush) {
        CBrush3D *pbr = penCurrent->GetBrush();
        if (pbr != NULL) {
          pbr->FromObject3D_t(o3dEntity);
          pbr->CalculateBoundingBoxes();
          // a separate object whose only polygons were slivers the import
          // collapsed would be an empty WorldBase; nothing links to one
          INDEX ctKept = 0;
          CBrushMip *pbm = pbr->GetFirstMip();
          if (pbm != NULL) {
            FOREACHINDYNAMICARRAY(pbm->bm_abscSectors, CBrushSector, itbsc) {
              ctKept += itbsc->bsc_abpoPolygons.Count();
            }
          }
          if (ctKept == 0 && strcmp(penCurrent->GetClass()->ec_pdecDLLClass->dec_strName, "WorldBase") == 0) {
            aEntities.back() = NULL;
            // its own links go with it
            for (size_t iLink = aLinks.size(); iLink-- > 0; ) {
              if (aLinks[iLink].pl_pen == penCurrent) { aLinks.erase(aLinks.begin() + iLink); }
            }
            penCurrent->Destroy();
            ctEmptyObjects++;
          }
        }
      }
      penCurrent = NULL;
      posc = NULL;

    } else if ((str[0] == 102 || str[0] == 98 || str[0] == 105 || str[0] == 99)   // f, b, i, c
               && strcmp(str + 1, "prop") == 0 && penCurrent != NULL) {
      // fprop/bprop/iprop/cprop <value> <property name to end of line>
      char strValue[64] = "", strName[128] = "";
      ReadToken(f, strValue, sizeof(strValue));
      ReadRestOfLine(f, strName, sizeof(strName));
      CEntityProperty *pep = FindProp(penCurrent, strName);
      if (pep != NULL) {
        if (str[0] == 102) {          // f: FLOAT
          ENTITYPROPERTY(penCurrent, pep->ep_slOffset, FLOAT) = (FLOAT)atof(strValue);
        } else if (str[0] == 99) {    // c: COLOR, hex RRGGBBAA
          ENTITYPROPERTY(penCurrent, pep->ep_slOffset, COLOR) = (COLOR)strtoul(strValue, NULL, 16);
        } else {                      // b, i: BOOL and INDEX are both INDEX wide
          ENTITYPROPERTY(penCurrent, pep->ep_slOffset, INDEX) = (INDEX)atoi(strValue);
        }
        ctProps++;
      }

    } else if (strcmp(str, "aprop") == 0 && penCurrent != NULL) {
      // aprop <x> <y> <z> <property name>: an angle property facing that direction
      double d[3] = { 0.0, 0.0, -1.0 };
      fscanf(f, "%lf %lf %lf", &d[0], &d[1], &d[2]);
      char strName[128] = "";
      ReadRestOfLine(f, strName, sizeof(strName));
      CEntityProperty *pep = FindProp(penCurrent, strName);
      if (pep != NULL) {
        ANGLE3D a;
        DirectionVectorToAnglesNoSnap(FLOAT3D((FLOAT)d[0], (FLOAT)d[1], (FLOAT)d[2]).Normalize(), a);
        ENTITYPROPERTY(penCurrent, pep->ep_slOffset, ANGLE3D) = a;
        ctProps++;
      }

    } else if (strcmp(str, "sprop") == 0 && penCurrent != NULL) {
      // sprop <property name> = <string to end of line>
      char strLine[512] = "";
      ReadRestOfLine(f, strLine, sizeof(strLine));
      char *pchEq = strstr(strLine, " = ");
      if (pchEq != NULL) {
        *pchEq = 0;
        CEntityProperty *pep = FindProp(penCurrent, strLine);
        if (pep != NULL) {
          ENTITYPROPERTY(penCurrent, pep->ep_slOffset, CTString) = CTString(pchEq + 3);
          ctProps++;
        }
      }

    } else if (strcmp(str, "eprop") == 0 && penCurrent != NULL) {
      char strValue[64] = "", strName[128] = "";
      ReadToken(f, strValue, sizeof(strValue));
      ReadRestOfLine(f, strName, sizeof(strName));
      SPendingLink pl;
      pl.pl_pen = penCurrent;
      pl.pl_strName = strName;
      pl.pl_iTarget = (INDEX)atoi(strValue);
      aLinks.push_back(pl);

    } else if (strcmp(str, "verts") == 0) {
      INDEX ct = 0;
      fscanf(f, "%d", &ct);
      if (penCurrent != NULL && posc == NULL) {      // sector of this entity
        posc = o3dEntity.ob_aoscSectors.New(1);
        posc->osc_strName = "brush";
        posc->osc_colAmbient = colBrushAmbient;
        posc->osc_ulFlags[0] = posc->osc_ulFlags[1] = posc->osc_ulFlags[2] = 0;
        bEntityHasBrush = TRUE;
      }
      if (posc == NULL && iWorldSector < 0) { printf("verts outside a sector%c", NLCH); return 1; }
      aPool.resize(ct);
      for (INDEX i = 0; i < ct; i++) {
        double x = 0.0, y = 0.0, z = 0.0;
        fscanf(f, "%lf %lf %lf", &x, &y, &z);
        aPool[i] = DOUBLE3D(x, y, z);
      }
      ctVertices += ct;

    } else if (strcmp(str, "mat") == 0 && (posc != NULL || iWorldSector >= 0)) {
      char strTex[800] = "";
      ReadRestOfLine(f, strTex, sizeof(strTex));
      if (posc != NULL) {
        *posc->osc_aomtMaterials.New(1) = MakeMaterial(CTString(strTex));
      } else {
        aWorldSectors[iWorldSector].ws_astrMaterials.push_back(CTString(strTex));
      }

    } else if (strcmp(str, "poly") == 0 && (posc != NULL || iWorldSector >= 0)) {
      INDEX iMaterial = 0;
      fscanf(f, "%d", &iMaterial);
      INDEX ctMaterials = posc != NULL ? posc->osc_aomtMaterials.Count()
                                       : (INDEX)aWorldSectors[iWorldSector].ws_astrMaterials.size();
      pending.pp_posc = posc;
      pending.pp_iWorldSector = posc != NULL ? -1 : iWorldSector;
      pending.pp_bSplit = FALSE;
      pending.pp_iMaterial = iMaterial;
      pending.pp_aLoops.assign(1, std::vector<DOUBLE3D>());
      pending.pp_aSplit.clear();
      pending.pp_bMapped = FALSE;
      pending.pp_ulFlags = ulPolygonFlags;
      for (INDEX iLayer = 0; iLayer < 3; iLayer++) { pending.pp_aLayers[iLayer].pl_bSet = FALSE; }
      pending.pp_bValid = ReadLoop(f, aPool, pending.pp_aLoops[0])
                       && iMaterial >= 0 && iMaterial < ctMaterials;

    } else if (strcmp(str, "flags") == 0) {
      // flags <hex>: the polygon's flags, e.g. 1 (OPOF_PORTAL) or 20000 (BPOF_INVISIBLE)
      unsigned long ulFlags = 0;
      if (fscanf(f, "%lx", &ulFlags) == 1 && (pending.pp_posc != NULL || pending.pp_iWorldSector >= 0)) {
        pending.pp_ulFlags = ulPolygonFlags | (ULONG)ulFlags;
      }

    } else if (strcmp(str, "uv") == 0) {
      // uv <mexW> <mexH> <ax> <ay> <az> <a0> <bx> <by> <bz> <b0>
      double d[10];
      INDEX ctRead = 0;
      for (INDEX i = 0; i < 10; i++) { ctRead += fscanf(f, "%lf", &d[i]); }
      if ((pending.pp_posc != NULL || pending.pp_iWorldSector >= 0) && ctRead == 10 && d[0] > 0 && d[1] > 0) {
        pending.pp_fMexW = d[0];
        pending.pp_fMexH = d[1];
        pending.pp_vA = DOUBLE3D(d[2], d[3], d[4]);
        pending.pp_fA0 = d[5];
        pending.pp_vB = DOUBLE3D(d[6], d[7], d[8]);
        pending.pp_fB0 = d[9];
        pending.pp_bMapped = TRUE;
      }

    } else if (strcmp(str, "layer") == 0) {
      // layer <slot> <scroll> <blend> <flags-hex> <color-hex> <mexW> <mexH> <ax> <ay> <az> <a0> <bx> <by> <bz> <b0>
      INDEX iSlot = 0;
      unsigned int uScroll = 0, uBlend = 0, uFlags = 0;
      unsigned long ulColor = 0;
      double d[10];
      INDEX ctRead = fscanf(f, "%d %u %u %x %lx", &iSlot, &uScroll, &uBlend, &uFlags, &ulColor);
      for (INDEX i = 0; i < 10; i++) { ctRead += fscanf(f, "%lf", &d[i]); }
      if ((pending.pp_posc != NULL || pending.pp_iWorldSector >= 0) && ctRead == 15
          && iSlot >= 0 && iSlot <= 2 && d[0] > 0 && d[1] > 0) {
        SPendingLayer &pl = pending.pp_aLayers[iSlot];
        pl.pl_ubScroll = (UBYTE)uScroll;
        pl.pl_ubBlend = (UBYTE)uBlend;
        pl.pl_ubFlags = (UBYTE)uFlags;
        pl.pl_colColor = (COLOR)ulColor;
        pl.pl_fMexW = d[0];
        pl.pl_fMexH = d[1];
        pl.pl_vA = DOUBLE3D(d[2], d[3], d[4]);
        pl.pl_fA0 = d[5];
        pl.pl_vB = DOUBLE3D(d[6], d[7], d[8]);
        pl.pl_fB0 = d[9];
        pl.pl_bSet = TRUE;
      }

    } else if (strcmp(str, "hole") == 0 || strcmp(str, "split") == 0) {
      std::vector<DOUBLE3D> av;
      BOOL bValid = ReadLoop(f, aPool, av);
      if (pending.pp_posc != NULL || pending.pp_iWorldSector >= 0) {
        pending.pp_bValid = pending.pp_bValid && bValid;
        (str[0] == 104 ? pending.pp_aLoops : pending.pp_aSplit).push_back(av);   // h: hole
      }
    }
  }
  FlushPolygon(pending, penScratch, ctPolygons, ctHoles, ctSplit, aWorldSectors, aWorldPolygons);
  fclose(f);

  // the static world: one WorldBase carrying every world sector (the level's
  // largest connected piece and its liquid surfaces; the other pieces came
  // as WorldBase entity blocks)
  CTFileName fnmBase = CTFileName(CTString("Classes") + SEP + "WorldBase.ecl");
  CEntity *penBase = wo.CreateEntity_t(plOrigin, fnmBase);
  // zoning and anchored, as the editor makes the first WorldBase of a new
  // world (EFirstWorldBase); Main turns both into entity flags
  const char *astrBaseFlags[] = { "Zoning", "Anchored" };
  for (INDEX iFlag = 0; iFlag < 2; iFlag++) {
    CEntityProperty *pep = FindProp(penBase, astrBaseFlags[iFlag]);
    if (pep == NULL) { printf("WorldBase has no %s property%c", astrBaseFlags[iFlag], NLCH); return 3; }
    ENTITYPROPERTY(penBase, pep->ep_slOffset, INDEX) = TRUE;
  }
  penBase->Initialize();
  CBrush3D *pbr = penBase->GetBrush();
  if (pbr == NULL) { printf("WorldBase has no brush%c", NLCH); return 3; }
  clock_t tStart = clock();
  INDEX ctPasses = 0;
  SplitWhereInvalid(aWorldSectors, aWorldPolygons, penScratch, ctPasses);
  INDEX ctWorldPolygons, ctWorldHoles, ctWorldSplit;
  BuildWorldObject(o3dWorld, aWorldSectors, aWorldPolygons, FALSE, ctWorldPolygons, ctWorldHoles, ctWorldSplit);
  ctPolygons += ctWorldPolygons;
  ctHoles += ctWorldHoles;
  ctSplit += ctWorldSplit;
  pbr->FromObject3D_t(o3dWorld);
  pbr->CalculateBoundingBoxes();
  const double dBrush = double(clock() - tStart) / CLOCKS_PER_SEC;

  // entity-pointer properties, now that every entity exists
  INDEX ctLinked = 0;
  for (size_t i = 0; i < aLinks.size(); i++) {
    const SPendingLink &pl = aLinks[i];
    if (pl.pl_iTarget < 0 || pl.pl_iTarget >= (INDEX)aEntities.size()) { continue; }
    CEntityProperty *pep = FindProp(pl.pl_pen, pl.pl_strName);
    if (pep == NULL) { continue; }
    ENTITYPROPERTY(pl.pl_pen, pep->ep_slOffset, CEntityPointer) = aEntities[pl.pl_iTarget];
    ctLinked++;
  }

  printf("%s: %d sectors, %d polygons (%d holes; %d polygons SE1 cannot triangulate put in as convex pieces, found in %d passes), %d entities, %d properties, %d links; %d objects left empty by the import removed%c",
         strWorld, ctSectors, ctPolygons, ctHoles, ctSplit, ctPasses, (INDEX)aEntities.size(), ctProps, ctLinked, ctEmptyObjects, NLCH);

  // Shadow maps from the lights, as the editor's batch conversion recalculates
  // a world's (WorldEditor.cpp): the lights were created before the brushes
  // they fall on, so every layer is found again first.
  tStart = clock();
  wo.DiscardAllShadows();
  wo.CalculateDirectionalShadows();
  wo.CalculateNonDirectionalShadows();
  const double dShadows = double(clock() - tStart) / CLOCKS_PER_SEC;

  CTFileName fnmWorld = CTFileName(CTString("Levels") + SEP + "NextEncounter" + SEP + strWorld + ".wld");
  _mkdir(CTString(_fnmApplicationPath + "Levels"));
  _mkdir(CTString(_fnmApplicationPath + "Levels" + SEP + "NextEncounter"));
  wo.SetName(strWorld);
  tStart = clock();
  wo.Save_t(fnmWorld);
  printf("  brush %.1fs, shadows %.1fs, save %.1fs%c", dBrush, dShadows, double(clock() - tStart) / CLOCKS_PER_SEC, NLCH);

  INDEX ctSec = 0, ctPol = 0;
  CountBrushes(wo, ctSec, ctPol);
  printf("wrote %s: %d entities, %d brush sectors, %d brush polygons%c",
         (const char *)fnmWorld, wo.wo_cenEntities.Count(), ctSec, ctPol, NLCH);
  return 0;
}

static int CheckWorld(const char *strWorld)
{
  CWorld wo;
  wo.Load_t(CTString(strWorld));
  INDEX ctSec = 0, ctPol = 0;
  CountBrushes(wo, ctSec, ctPol);
  printf("loaded %s: name [%s], %d entities, %d brush sectors, %d polygons%c",
         strWorld, (const char *)wo.GetName(), wo.wo_cenEntities.Count(), ctSec, ctPol, NLCH);
  FOREACHINDYNAMICCONTAINER(wo.wo_cenEntities, CEntity, iten) {
    CEntity *pen = iten;
    const char *strClass = pen->GetClass()->ec_pdecDLLClass->dec_strName;
    const FLOAT3D &v = pen->GetPlacement().pl_PositionVector;
    // en_pbrBrush shares a union with the model pointer: only read it on a brush
    const BOOL bBrush = (pen->en_RenderType == CEntity::RT_BRUSH
                      || pen->en_RenderType == CEntity::RT_FIELDBRUSH);
    CBrushMip *pbm = bBrush && pen->en_pbrBrush != NULL ? pen->en_pbrBrush->GetFirstMip() : NULL;
    if (pbm == NULL) {
      printf("  entity %-24s at %8.2f %8.2f %8.2f%s%c", strClass, v(1), v(2), v(3),
             bBrush ? "  (brush with no mip)" : "", NLCH);
      continue;
    }
    FLOATaabbox3D box = pbm->bm_boxBoundingBox;
    printf("  brush  %-24s at %8.2f %8.2f %8.2f  box %.2f %.2f %.2f .. %.2f %.2f %.2f%s%c",
           strClass, v(1), v(2), v(3),
           box.Min()(1), box.Min()(2), box.Min()(3),
           box.Max()(1), box.Max()(2), box.Max()(3),
           (pen->en_ulFlags & ENF_ZONING) ? "  zoning" : "", NLCH);
  }
  // do entity links survive the round trip?
  INDEX ctTargets = 0, ctTargetsSet = 0;
  FOREACHINDYNAMICCONTAINER(wo.wo_cenEntities, CEntity, itenL) {
    CEntityProperty *pep = itenL->PropertyForName(CTString("Target"));
    if (pep == NULL || pep->ep_eptType != CEntityProperty::EPT_ENTITYPTR) { continue; }
    ctTargets++;
    CEntityPointer &pen = ENTITYPROPERTY(&*itenL, pep->ep_slOffset, CEntityPointer);
    if (pen != NULL) { ctTargetsSet++; }
  }
  printf("  Target properties: %d, set after loading: %d%c", ctTargets, ctTargetsSet, NLCH);
  // every entity pointer property, through each class's bases
  INDEX ctLinksSet = 0;
  FOREACHINDYNAMICCONTAINER(wo.wo_cenEntities, CEntity, itenA) {
    for (CDLLEntityClass *pdec = itenA->GetClass()->ec_pdecDLLClass; pdec != NULL; pdec = pdec->dec_pdecBase) {
      for (INDEX iProp = 0; iProp < pdec->dec_ctProperties; iProp++) {
        const CEntityProperty &ep = pdec->dec_aepProperties[iProp];
        if (ep.ep_eptType == CEntityProperty::EPT_ENTITYPTR
         && ENTITYPROPERTY(&*itenA, ep.ep_slOffset, CEntityPointer) != NULL) {
          ctLinksSet++;
        }
      }
    }
  }
  printf("  entity links set after loading: %d%c", ctLinksSet, NLCH);
  // portals: link them as the renderer does, then count what each reaches
  wo.wo_baBrushes.LinkPortalsAndSectors();
  INDEX ctZoningSectors = 0, ctPortals = 0, ctPortalsLinked = 0, ctSectorsReached = 0, ctInvisible = 0;
  FOREACHINDYNAMICCONTAINER(wo.wo_cenEntities, CEntity, itenP) {
    if (!(itenP->en_ulFlags & ENF_ZONING) || itenP->en_RenderType != CEntity::RT_BRUSH) { continue; }
    CBrushMip *pbmP = itenP->en_pbrBrush != NULL ? itenP->en_pbrBrush->GetFirstMip() : NULL;
    if (pbmP == NULL) { continue; }
    FOREACHINDYNAMICARRAY(pbmP->bm_abscSectors, CBrushSector, itbsc) {
      // content cells (the region brush) are counted below, not as rooms
      if (itbsc->GetContentType() != 0) { continue; }
      ctZoningSectors++;
      BOOL bReached = FALSE;
      FOREACHSRCOFDST(itbsc->bsc_rdOtherSidePortals, CBrushPolygon, bpo_rsOtherSideSectors, pbpo)
        bReached = TRUE;
      ENDFOR
      if (bReached) { ctSectorsReached++; }
      FOREACHINSTATICARRAY(itbsc->bsc_abpoPolygons, CBrushPolygon, itbpo) {
        if (itbpo->bpo_ulFlags & BPOF_INVISIBLE) { ctInvisible++; }
        if (!(itbpo->bpo_ulFlags & BPOF_PORTAL)) { continue; }
        ctPortals++;
        BOOL bLinked = FALSE;
        FOREACHDSTOFSRC(itbpo->bpo_rsOtherSideSectors, CBrushSector, bsc_rdOtherSidePortals, pbscOther)
          if (pbscOther != &*itbsc) { bLinked = TRUE; }
        ENDFOR
        if (bLinked) { ctPortalsLinked++; }
      }
    }
  }
  printf("  zoning sectors: %d, reached through a portal: %d; portals: %d, linked to another sector: %d; invisible polygons: %d%c",
         ctZoningSectors, ctSectorsReached, ctPortals, ctPortalsLinked, ctInvisible, NLCH);
  // content cells and touch fields: a sector's polygons face into it when its
  // signed volume, summed over its triangles against their planes, is
  // negative. That holds for any closed shape, convex or not.
  INDEX ctCells = 0, ctCellsInward = 0, ctCellsHaze = 0, ctCellsHazeSet = 0;
  INDEX ctFields = 0, ctFieldsInward = 0;
  FOREACHINDYNAMICCONTAINER(wo.wo_cenEntities, CEntity, itenC) {
    const BOOL bField = itenC->en_RenderType == CEntity::RT_FIELDBRUSH;
    const BOOL bZoning = itenC->en_RenderType == CEntity::RT_BRUSH && (itenC->en_ulFlags & ENF_ZONING);
    if ((!bField && !bZoning) || itenC->en_pbrBrush == NULL) { continue; }
    CBrushMip *pbmC = itenC->en_pbrBrush->GetFirstMip();
    if (pbmC == NULL) { continue; }
    FOREACHINDYNAMICARRAY(pbmC->bm_abscSectors, CBrushSector, itbscC) {
      if (bZoning && itbscC->GetContentType() == 0) { continue; }
      DOUBLE dVolume = 0.0;
      FOREACHINSTATICARRAY(itbscC->bsc_abpoPolygons, CBrushPolygon, itbpoC) {
        itbpoC->Triangulate();
        const FLOAT3D &vN = (const FLOAT3D &)itbpoC->bpo_pbplPlane->bpl_plAbsolute;
        for (INDEX iE = 0; iE + 2 < itbpoC->bpo_aiTriangleElements.Count(); iE += 3) {
          const FLOAT3D &v0 = itbpoC->bpo_apbvxTriangleVertices[itbpoC->bpo_aiTriangleElements[iE]]->bvx_vAbsolute;
          const FLOAT3D &v1 = itbpoC->bpo_apbvxTriangleVertices[itbpoC->bpo_aiTriangleElements[iE + 1]]->bvx_vAbsolute;
          const FLOAT3D &v2 = itbpoC->bpo_apbvxTriangleVertices[itbpoC->bpo_aiTriangleElements[iE + 2]]->bvx_vAbsolute;
          const DOUBLE dArea = ((v1 - v0) * (v2 - v0)).Length() / 2.0;
          dVolume += dArea * (vN % v0) / 3.0;
        }
      }
      if (bField) {
        ctFields++;
        if (dVolume < 0) { ctFieldsInward++; }
        continue;
      }
      ctCells++;
      if (dVolume < 0) { ctCellsInward++; }
      if (itbscC->GetHazeType() != 0) {
        ctCellsHaze++;
        CHazeParameters hp;
        FLOAT3D vDir(0, 0, -1);
        if (itenC->GetHaze(itbscC->GetHazeType(), hp, vDir)) { ctCellsHazeSet++; }
      }
    }
  }
  printf("  content cells: %d, facing inward: %d; with haze: %d, haze marker found: %d%c",
         ctCells, ctCellsInward, ctCellsHaze, ctCellsHazeSet, NLCH);
  printf("  field brush sectors: %d, facing inward: %d%c", ctFields, ctFieldsInward, NLCH);
  return (ctSec > 0 && ctPol > 0) ? 0 : 2;
}

static int RayTest(const char *strWorld)
{
  CWorld wo;
  wo.Load_t(CTString(strWorld));

  FLOATaabbox3D box;
  BOOL bAny = FALSE;
  FOREACHINDYNAMICCONTAINER(wo.wo_cenEntities, CEntity, iten) {
    if (iten->en_pbrBrush == NULL) { continue; }
    if (iten->en_RenderType != CEntity::RT_BRUSH) { continue; }
    CBrushMip *pbmRay = iten->en_pbrBrush->GetFirstMip();
    if (pbmRay == NULL) { continue; }
    // the level itself: the visible polygons of every static WorldBase brush
    // (each separate object is a WorldBase of its own; the rooms' portals
    // and invisible closing polygons reach past the geometry)
    if (strcmp(iten->GetClass()->ec_pdecDLLClass->dec_strName, "WorldBase") != 0) { continue; }
    FOREACHINDYNAMICARRAY(pbmRay->bm_abscSectors, CBrushSector, itbscRay) {
      FOREACHINSTATICARRAY(itbscRay->bsc_abpoPolygons, CBrushPolygon, itbpoRay) {
        if (itbpoRay->bpo_ulFlags & (BPOF_PORTAL | BPOF_INVISIBLE)) { continue; }
        if (!bAny) {
          box = itbpoRay->bpo_boxBoundingBox;
          bAny = TRUE;
        } else {
          box |= itbpoRay->bpo_boxBoundingBox;
        }
      }
    }
  }
  if (!bAny) { printf("no brush in %s\n", strWorld); return 2; }

  const INDEX ctSide = 24;
  const FLOAT fTop = box.Max()(2) + 16.0f;
  const FLOAT fBottom = box.Min()(2) - 16.0f;
  INDEX ctHit = 0, ctTotal = 0;
  FLOAT fSumDrop = 0.0f;
  INDEX ctFacing = 0;
  for (INDEX ix = 0; ix < ctSide; ix++) {
    for (INDEX iz = 0; iz < ctSide; iz++) {
      FLOAT fX = Lerp(box.Min()(1), box.Max()(1), (ix + 0.5f) / ctSide);
      FLOAT fZ = Lerp(box.Min()(3), box.Max()(3), (iz + 0.5f) / ctSide);
      CCastRay crRay(NULL, FLOAT3D(fX, fTop, fZ), FLOAT3D(fX, fBottom, fZ));
      crRay.cr_bHitPortals = FALSE;
      crRay.cr_bHitTranslucentPortals = FALSE;
      wo.CastRay(crRay);
      // closing polygons of the rooms are invisible: look past them, casting
      // on from just below each one
      FLOAT fDropped = 0.0f;
      for (INDEX iPass = 0; iPass < 256 && crRay.cr_penHit != NULL && crRay.cr_pbpoBrushPolygon != NULL
           && (crRay.cr_pbpoBrushPolygon->bpo_ulFlags & BPOF_INVISIBLE); iPass++) {
        CBrushPolygon *pbpoIgnore = crRay.cr_pbpoBrushPolygon;
        fDropped += crRay.cr_fHitDistance;
        FLOAT fFrom = fTop - fDropped;
        crRay = CCastRay(NULL, FLOAT3D(fX, fFrom, fZ), FLOAT3D(fX, fBottom, fZ));
        crRay.cr_bHitPortals = FALSE;
        crRay.cr_bHitTranslucentPortals = FALSE;
        crRay.cr_pbpoIgnore = pbpoIgnore;
        wo.CastRay(crRay);
      }
      if (crRay.cr_penHit != NULL) { crRay.cr_fHitDistance += fDropped; }
      ctTotal++;
      if (crRay.cr_penHit != NULL) {
        ctHit++;
        fSumDrop += crRay.cr_fHitDistance;
        // a ray from the sky should meet surfaces that face it (normal up)
        if (crRay.cr_pbpoBrushPolygon != NULL) {
          if (crRay.cr_pbpoBrushPolygon->bpo_pbplPlane->bpl_plAbsolute(2) > 0.0f) { ctFacing++; }
        }
      }
    }
  }
  printf("%s: %d of %d downward rays hit, %d of them on an up-facing polygon, mean drop %.1f%c",
         strWorld, ctHit, ctTotal, ctFacing, ctHit ? fSumDrop / ctHit : 0.0f, NLCH);
  return ctHit > 0 ? 0 : 2;
}

// What the editor reports as bad triangulation: BPOF_INVALIDTRIANGLES after
// CBrushSector::Triangulate(). Count it for every brush polygon.
static int TriangulationTest(const char *strWorld)
{
  CWorld wo;
  wo.Load_t(CTString(strWorld));
  INDEX ctPolygons = 0, ctInvalid = 0, ctPortal = 0, ctInvisible = 0;
  INDEX ctInvalidPortal = 0, ctInvalidInvisible = 0;
  INDEX ctUp = 0, ctDown = 0;        // horizontal polygons facing up / down
  FOREACHINDYNAMICCONTAINER(wo.wo_cenEntities, CEntity, iten) {
    if (iten->en_RenderType != CEntity::RT_BRUSH && iten->en_RenderType != CEntity::RT_FIELDBRUSH) {
      continue;
    }
    CBrushMip *pbm = iten->en_pbrBrush->GetFirstMip();
    if (pbm == NULL) { continue; }
    FOREACHINDYNAMICARRAY(pbm->bm_abscSectors, CBrushSector, itbsc) {
      FOREACHINSTATICARRAY(itbsc->bsc_abpoPolygons, CBrushPolygon, itbpo) {
        itbpo->bpo_apbvxTriangleVertices.Clear();     // force a fresh triangulation
        itbpo->Triangulate();
        ctPolygons++;
        if (itbpo->bpo_ulFlags & BPOF_INVALIDTRIANGLES) {
          // name the first few: edge count and the edges themselves
          if (ctInvalid < 8) {
            printf("  invalid: %d edges, %d triangles%c", itbpo->bpo_abpePolygonEdges.Count(),
                   itbpo->bpo_aiTriangleElements.Count() / 3, NLCH);
            FOREACHINSTATICARRAY(itbpo->bpo_abpePolygonEdges, CBrushPolygonEdge, itbpe) {
              CBrushVertex *pbvx0, *pbvx1;
              itbpe->GetVertices(pbvx0, pbvx1);
              const DOUBLE3D &v0 = pbvx0->bvx_vdPreciseRelative;
              const DOUBLE3D &v1 = pbvx1->bvx_vdPreciseRelative;
              printf("    %.4f %.4f %.4f -> %.4f %.4f %.4f%c", v0(1), v0(2), v0(3), v1(1), v1(2), v1(3), NLCH);
            }
          }
          ctInvalid++;
          if (itbpo->bpo_ulFlags & BPOF_PORTAL) { ctInvalidPortal++; }
          if (itbpo->bpo_ulFlags & BPOF_INVISIBLE) { ctInvalidInvisible++; }
        }
        if (itbpo->bpo_ulFlags & BPOF_PORTAL) { ctPortal++; }
        if (itbpo->bpo_ulFlags & BPOF_INVISIBLE) { ctInvisible++; }
        const FLOAT3D &vN = itbpo->bpo_pbplPlane->bpl_plAbsolute;
        if (vN(2) > 0.9f) { ctUp++; } else if (vN(2) < -0.9f) { ctDown++; }
      }
    }
  }
  printf("%s: %d polygons, %d invalid triangles (%d portals, %d invisible), %d portals, %d invisible; horizontal: %d up, %d down%c",
         strWorld, ctPolygons, ctInvalid, ctInvalidPortal, ctInvalidInvisible, ctPortal, ctInvisible, ctUp, ctDown, NLCH);
  return 0;
}

// Every polygon of every brush in a world, as text: the brushes only, so a
// world whose entity classes are not installed still reads. Missing textures
// become the editor default unless a replacement table names a stand-in. For comparing an original SE1 world's polygons
// with the ones rebuilt from the disc.
//   brush <i> mip <m> sectors <n>
//   sector <name>
//   poly <flags-hex> <nx> <ny> <nz> <d> <edges> <uos> <uot> <vos> <vot> <uoff> <voff> <tex0> <tex1> <tex2>
//   mv <origin xyz> <u axis xyz> <v axis xyz>  the plane's default mapping vectors
//   layer <i> <scroll> <blend> <flags-hex> <color-hex> <uos> <uot> <vos> <vot> <uoff> <voff> <tex>
//                                 each texture layer's settings and mapping
//   e <x0> <y0> <z0> <x1> <y1> <z1>
//   t <x y z> <x y z> <x y z>     one triangle, as the polygon's triangulation lists it
static int DumpBrushes(const char *strWorld, const char *strOut)
{
  // A replacement table (Data/BaseForReplacingFiles.txt), if present, stands a
  // distinct installed texture in for each missing one, so names survive.
  CTString strBase = CTString("Data") + SEP + "BaseForReplacingFiles.txt";
  _pShell->SetINDEX("wed_bUseBaseForReplacement", FileExists(CTFileName(strBase)) ? 1 : 0);
  _pShell->SetINDEX("wed_bUseGenericTextureReplacement", 1);
  CWorld wo;
  wo.LoadBrushes_t(CTString(strWorld));
  FILE *f = fopen(strOut, "w");
  if (f == NULL) { printf("cannot write %s%c", strOut, NLCH); return 1; }
  INDEX iBrush = 0, ctPolygons = 0;
  FOREACHINDYNAMICARRAY(wo.wo_baBrushes.ba_abrBrushes, CBrush3D, itbr) {
    INDEX iMip = 0;
    FOREACHINLIST(CBrushMip, bm_lnInBrush, itbr->br_lhBrushMips, itbm) {
      fprintf(f, "brush %d mip %d sectors %d%c", iBrush, iMip, itbm->bm_abscSectors.Count(), NLCH);
      FOREACHINDYNAMICARRAY(itbm->bm_abscSectors, CBrushSector, itbsc) {
        fprintf(f, "sector %s%c", (const char *)itbsc->bsc_strName, NLCH);
        FOREACHINSTATICARRAY(itbsc->bsc_abpoPolygons, CBrushPolygon, itbpo) {
          const DOUBLEplane3D &pl = itbpo->bpo_pbplPlane->bpl_pldPreciseRelative;
          const CMappingDefinition &md = itbpo->bpo_abptTextures[0].bpt_mdMapping;
          fprintf(f, "poly %08lx %.9g %.9g %.9g %.9g %d %.9g %.9g %.9g %.9g %.9g %.9g",
                  (unsigned long)itbpo->bpo_ulFlags, pl(1), pl(2), pl(3), pl.Distance(),
                  itbpo->bpo_abpePolygonEdges.Count(),
                  md.md_fUoS, md.md_fUoT, md.md_fVoS, md.md_fVoT, md.md_fUOffset, md.md_fVOffset);
          for (INDEX iLayer = 0; iLayer < 3; iLayer++) {
            CTextureObject &to = itbpo->bpo_abptTextures[iLayer].bpt_toTexture;
            fprintf(f, " %s", to.GetData() != NULL ? (const char *)to.GetName() : "-");
          }
          fprintf(f, "%c", NLCH);
          // the default mapping vectors, exactly as the sector builds them
          // (CBrushSector: FromPlane of the plane in single precision)
          CMappingVectors mvDefault;
          mvDefault.FromPlane(DOUBLEtoFLOAT(itbpo->bpo_pbplPlane->bpl_pldPreciseRelative));
          fprintf(f, "mv %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g %.9g%c",
                  mvDefault.mv_vO(1), mvDefault.mv_vO(2), mvDefault.mv_vO(3),
                  mvDefault.mv_vU(1), mvDefault.mv_vU(2), mvDefault.mv_vU(3),
                  mvDefault.mv_vV(1), mvDefault.mv_vV(2), mvDefault.mv_vV(3), NLCH);
          for (INDEX iLayer = 0; iLayer < 3; iLayer++) {
            CBrushPolygonTexture &bpt = itbpo->bpo_abptTextures[iLayer];
            const CMappingDefinition &mdL = bpt.bpt_mdMapping;
            fprintf(f, "layer %d %d %d %x %08lx %.9g %.9g %.9g %.9g %.9g %.9g %s%c", iLayer,
                    bpt.s.bpt_ubScroll, bpt.s.bpt_ubBlend, bpt.s.bpt_ubFlags, (unsigned long)bpt.s.bpt_colColor,
                    mdL.md_fUoS, mdL.md_fUoT, mdL.md_fVoS, mdL.md_fVoT, mdL.md_fUOffset, mdL.md_fVOffset,
                    bpt.bpt_toTexture.GetData() != NULL ? (const char *)bpt.bpt_toTexture.GetName() : "-", NLCH);
          }
          FOREACHINSTATICARRAY(itbpo->bpo_abpePolygonEdges, CBrushPolygonEdge, itbpe) {
            CBrushVertex *pbvx0, *pbvx1;
            itbpe->GetVertices(pbvx0, pbvx1);
            const DOUBLE3D &v0 = pbvx0->bvx_vdPreciseRelative;
            const DOUBLE3D &v1 = pbvx1->bvx_vdPreciseRelative;
            fprintf(f, "e %.5f %.5f %.5f %.5f %.5f %.5f%c", v0(1), v0(2), v0(3), v1(1), v1(2), v1(3), NLCH);
          }
          // the triangles as stored (triangulating first only if none are)
          itbpo->Triangulate();
          for (INDEX iElement = 0; iElement + 2 < itbpo->bpo_aiTriangleElements.Count(); iElement += 3) {
            fprintf(f, "t");
            for (INDEX iCorner = 0; iCorner < 3; iCorner++) {
              INDEX iVertex = itbpo->bpo_aiTriangleElements[iElement + iCorner];
              const DOUBLE3D &v = itbpo->bpo_apbvxTriangleVertices[iVertex]->bvx_vdPreciseRelative;
              fprintf(f, " %.5f %.5f %.5f", v(1), v(2), v(3));
            }
            fprintf(f, "%c", NLCH);
          }
          ctPolygons++;
        }
      }
      iMip++;
    }
    iBrush++;
  }
  fclose(f);
  printf("%s: %d brushes, %d polygons in all mips, written to %s%c", strWorld, iBrush, ctPolygons, strOut, NLCH);
  return 0;
}

// --lightmap <world> <out>: every visible brush polygon's shadow map as SE1
// mixes it, to set beside the level's own baked lightmap (tools/lightmap.py):
//   p <w> <h> <ox oy oz> <ux uy uz> <vx vy vz> <ntri>
//                                 texel (i, j)'s centre is o + i*u + j*v, absolute
//   t <x y z> <x y z> <x y z>     the polygon's triangles, absolute
//   m <hex RRGGBB per texel, row by row>
static int DumpLightmaps(const char *strWorld, const char *strOut)
{
  CWorld wo;
  wo.Load_t(CTString(strWorld));
  // every texel mixed, undithered, as it is stored
  _pShell->SetINDEX("shd_bAllowFlats", 0);
  _pShell->SetINDEX("shd_iForceFlats", 0);
  _pShell->SetINDEX("shd_iDithering", 0);
  _pShell->SetINDEX("shd_bFineQuality", 1);
  FILE *f = fopen(strOut, "w");
  if (f == NULL) { printf("cannot write %s%c", strOut, NLCH); return 1; }
  INDEX ctPolygons = 0, ctTexels = 0;
  FOREACHINDYNAMICCONTAINER(wo.wo_cenEntities, CEntity, iten) {
    if (iten->en_RenderType != CEntity::RT_BRUSH || iten->en_pbrBrush == NULL) { continue; }
    CBrushMip *pbm = iten->en_pbrBrush->GetFirstMip();
    if (pbm == NULL) { continue; }
    const FLOATmatrix3D &mRot = iten->en_mRotation;
    const FLOAT3D &vPos = iten->GetPlacement().pl_PositionVector;
    FOREACHINDYNAMICARRAY(pbm->bm_abscSectors, CBrushSector, itbsc) {
      FOREACHINSTATICARRAY(itbsc->bsc_abpoPolygons, CBrushPolygon, itbpo) {
        CBrushPolygon &bpo = *itbpo;
        if (bpo.bpo_ulFlags & (BPOF_INVISIBLE | BPOF_PORTAL)) { continue; }
        CBrushShadowMap &bsm = bpo.bpo_smShadowMap;
        const INDEX iMip = bsm.sm_iFirstMipLevel;
        if (bsm.sm_pulCachedShadowMap == NULL) { bsm.Cache(iMip); }
        if (bsm.sm_pulCachedShadowMap == NULL || bsm.sm_iFirstCachedMipLevel != iMip) { continue; }
        const PIX pixW = bsm.sm_mexWidth >> iMip, pixH = bsm.sm_mexHeight >> iMip;
        // the frame CLayerMixer::CalculateData walks the texels in
        const CMappingVectors &mv = bpo.bpo_pbplPlane->bpl_pwplWorking->wpl_mvRelative;
        MEX2D vmex0(-bsm.sm_mexOffsetX + (1 << (iMip - 1)), -bsm.sm_mexOffsetY + (1 << (iMip - 1)));
        MEX2D vmexU((1 << iMip) - bsm.sm_mexOffsetX + (1 << (iMip - 1)), -bsm.sm_mexOffsetY + (1 << (iMip - 1)));
        MEX2D vmexV(-bsm.sm_mexOffsetX + (1 << (iMip - 1)), (1 << iMip) - bsm.sm_mexOffsetY + (1 << (iMip - 1)));
        FLOAT3D vO, vU, vV;
        bpo.bpo_mdShadow.GetSpaceCoordinates(mv, vmex0, vO);
        bpo.bpo_mdShadow.GetSpaceCoordinates(mv, vmexU, vU);
        bpo.bpo_mdShadow.GetSpaceCoordinates(mv, vmexV, vV);
        vO = vO * mRot + vPos;
        vU = vU * mRot + vPos - vO;
        vV = vV * mRot + vPos - vO;
        bpo.Triangulate();
        const INDEX ctTriangles = bpo.bpo_aiTriangleElements.Count() / 3;
        fprintf(f, "p %d %d %.5f %.5f %.5f %.6f %.6f %.6f %.6f %.6f %.6f %d%c", pixW, pixH,
                vO(1), vO(2), vO(3), vU(1), vU(2), vU(3), vV(1), vV(2), vV(3), ctTriangles, NLCH);
        for (INDEX iElement = 0; iElement + 2 < bpo.bpo_aiTriangleElements.Count(); iElement += 3) {
          fprintf(f, "t");
          for (INDEX iCorner = 0; iCorner < 3; iCorner++) {
            INDEX iVertex = bpo.bpo_aiTriangleElements[iElement + iCorner];
            FLOAT3D v = bpo.bpo_apbvxTriangleVertices[iVertex]->bvx_vRelative * mRot + vPos;
            fprintf(f, " %.4f %.4f %.4f", v(1), v(2), v(3));
          }
          fprintf(f, "%c", NLCH);
        }
        // the finest mip sits at the start of the cache, each texel byte-swapped
        // to R, G, B, A in memory (CLayerMixer's ambient fill)
        // a full bright polygon is drawn without its shadow map, as a texel of
        // SE1's neutral 127 would draw it
        const BOOL bFullBright = (bpo.bpo_ulFlags & BPOF_FULLBRIGHT) != 0;
        const UBYTE *pub = (const UBYTE *)bsm.sm_pulCachedShadowMap;
        fprintf(f, "m ");
        for (PIX pix = 0; pix < pixW * pixH; pix++) {
          if (bFullBright) { fprintf(f, "7f7f7f"); continue; }
          fprintf(f, "%02x%02x%02x", pub[pix * 4], pub[pix * 4 + 1], pub[pix * 4 + 2]);
        }
        fprintf(f, "%c", NLCH);
        ctPolygons++;
        ctTexels += pixW * pixH;
      }
    }
  }
  fclose(f);
  printf("%s: %d polygons, %d shadow map texels, written to %s%c", strWorld, ctPolygons, ctTexels, strOut, NLCH);
  return 0;
}

// --tex <list>: one "<picture> <mex width>" per line, paths under the game
// root (tools/wldtex.py); each picture becomes a .tex beside it, with mipmaps
// as the editor's texture dialog makes them.
static int MakeTextures(const char *strList)
{
  FILE *f = fopen(strList, "rt");
  if (f == NULL) { printf("cannot open %s%c", strList, NLCH); return 1; }
  char strPicture[512];
  INDEX ctMade = 0, ctFailed = 0;
  while (ReadToken(f, strPicture, sizeof(strPicture))) {
    INDEX iMex = 0;
    if (fscanf(f, "%d", &iMex) != 1) { break; }
    try {
      // named explicitly: the short form writes the extension as ".TEX"
      CTFileName fnmPicture = CTString(strPicture);
      CreateTexture_t(fnmPicture, fnmPicture.FileDir() + fnmPicture.FileName() + ".tex", iMex, 16, FALSE);
      ctMade++;
    } catch (const char *strError) {
      printf("  %s: %s%c", strPicture, strError, NLCH);
      ctFailed++;
    }
  }
  fclose(f);
  printf("%d textures made, %d failed%c", ctMade, ctFailed, NLCH);
  return ctFailed ? 2 : 0;
}

// --mdl <script> <model>: build a model from a modeler script (.scr) with the
// engine's own CEditModel, as Serious Modeler's "create from script" does, and
// save the .mdl. Paths are under the game root. Frames are Wavefront OBJ
// (the fork's LoadAny3DFormat_t, pc/patches/obj-model-import.patch).
static int MakeModel(const char *strScript, const char *strModel)
{
  CEditModel *pem = new CEditModel;
  CTFileName fnmScript = CTString(strScript);
  pem->LoadFromScript_t(fnmScript);
  pem->Save_t(CTString(strModel));
  // the animation and collision box defines entity classes include
  CTFileName fnmModel = CTString(strModel);
  char strPrefix[256];
  // SaveIncludeFile_t adds the underscore itself: DUMDUMLARGE_ANIM_IDLE1
  _snprintf(strPrefix, sizeof(strPrefix), "%s", (const char *)fnmModel.FileName());
  _strupr(strPrefix);
  pem->SaveIncludeFile_t(fnmModel.NoExt() + ".h", CTString(strPrefix));
  CModelData &md = pem->edm_md;
  printf("%s: %d vertices, %d frames, %d mip models, %d animations%c",
         strModel, md.md_VerticesCt, md.md_FramesCt, md.md_MipCt, md.GetAnimsCt(), NLCH);
  delete pem;
  return 0;
}

// --mdlinfo <model> <out>: a model's animations, and each animation's first
// frame as vertex positions in model space, for checking against the source.
static int ModelInfo(const char *strModel, const char *strOut)
{
  CModelObject mo;
  mo.SetData_t(CTString(strModel));
  CModelData *pmd = (CModelData *)mo.GetData();
  FILE *f = fopen(strOut, "wt");
  if (f == NULL) { printf("cannot open %s%c", strOut, NLCH); return 1; }
  fprintf(f, "model %s vertices %d frames %d mips %d anims %d\n", strModel,
          pmd->md_VerticesCt, pmd->md_FramesCt, pmd->md_MipCt, pmd->GetAnimsCt());
  for (INDEX iAnim = 0; iAnim < pmd->GetAnimsCt(); iAnim++) {
    CAnimInfo ai;
    pmd->GetAnimInfo(iAnim, ai);
    fprintf(f, "anim %d %s frames %d secs %g\n", iAnim, ai.ai_AnimName, ai.ai_NumberOfFrames, ai.ai_SecsPerFrame);
  }
  for (INDEX iFrame = 0; iFrame < pmd->md_FramesCt; iFrame++) {
    FLOATaabbox3D box = mo.GetFrameBBox(iFrame);
    fprintf(f, "framebox %d %g %g %g %g %g %g\n", iFrame,
            box.Min()(1), box.Min()(2), box.Min()(3), box.Max()(1), box.Max()(2), box.Max()(3));
  }
  CStaticStackArray<FLOAT3D> avVertices;
  FLOATmatrix3D mIdentity;
  mIdentity.Diagonal(1.0f);
  mo.GetModelVertices(avVertices, mIdentity, FLOAT3D(0, 0, 0), 0.0f, 0.0f);
  for (INDEX iVtx = 0; iVtx < avVertices.Count(); iVtx++) {
    fprintf(f, "v %g %g %g\n", avVertices[iVtx](1), avVertices[iVtx](2), avVertices[iVtx](3));
  }
  // mip 0's polygons: each corner's vertex and its U,V in mex, in winding order
  ModelMipInfo &mmi = pmd->md_MipInfos[0];
  for (INDEX iPol = 0; iPol < mmi.mmpi_PolygonsCt; iPol++) {
    ModelPolygon &mp = mmi.mmpi_Polygons[iPol];
    fprintf(f, "poly %d surface %d", iPol, mp.mp_Surface);
    for (INDEX iCorner = 0; iCorner < mp.mp_PolygonVertices.Count(); iCorner++) {
      ModelTextureVertex &mtv = *mp.mp_PolygonVertices[iCorner].mpv_ptvTextureVertex;
      fprintf(f, " %d %d %d", mtv.mtv_iTransformedVertex, mtv.mtv_UV(1), mtv.mtv_UV(2));
    }
    fprintf(f, "\n");
  }
  fclose(f);
  printf("%s: %d vertices, %d frames, %d animations -> %s%c", strModel,
         pmd->md_VerticesCt, pmd->md_FramesCt, pmd->GetAnimsCt(), strOut, NLCH);
  return 0;
}

static int SubMain(int argc, char *argv[])
{
  // A non-empty game ID keeps the network library alive; with "" the engine
  // sets _pNetwork to NULL and CWorld::CreateEntity crashes on IsPredicting().
  SE_InitEngine("SeriousSam");
  printf("game root: %s\n", (const char *)_fnmApplicationPath);
  int iResult = 1;
  try {
    if (argc > 2 && strcmp(argv[1], "--check") == 0) { iResult = CheckWorld(argv[2]); }
    else if (argc > 2 && strcmp(argv[1], "--rays") == 0) { iResult = RayTest(argv[2]); }
    else if (argc > 2 && strcmp(argv[1], "--tri") == 0) { iResult = TriangulationTest(argv[2]); }
    else if (argc > 3 && strcmp(argv[1], "--dump") == 0) { iResult = DumpBrushes(argv[2], argv[3]); }
    else if (argc > 2 && strcmp(argv[1], "--tex") == 0) { iResult = MakeTextures(argv[2]); }
    else if (argc > 3 && strcmp(argv[1], "--lightmap") == 0) { iResult = DumpLightmaps(argv[2], argv[3]); }
    else if (argc > 3 && strcmp(argv[1], "--mdl") == 0) { iResult = MakeModel(argv[2], argv[3]); }
    else if (argc > 3 && strcmp(argv[1], "--mdlinfo") == 0) { iResult = ModelInfo(argv[2], argv[3]); }
    else if (argc > 1) { iResult = BuildFromSource(argv[1]); }
    else { iResult = SmokeTest(); }
  } catch (const char *strError) {
    printf("error: %s\n", strError);
    iResult = 1;
  }
  SE_EndEngine();
  return iResult;
}

// On an access violation, print the stack with symbols (the engine's PDBs sit
// in Sources/*/Release) and exit with 9: a crash in the middle of a batch then
// says where it was, as the heap corruption in shadow baking needed.
#include <windows.h>
#include <dbghelp.h>
#pragma comment(lib, "dbghelp.lib")
static LONG WINAPI CrashHandler(EXCEPTION_POINTERS *pep)
{
  if (pep->ExceptionRecord->ExceptionCode != EXCEPTION_ACCESS_VIOLATION) { return EXCEPTION_CONTINUE_SEARCH; }
  HANDLE hProcess = GetCurrentProcess();
  SymSetOptions(SYMOPT_UNDNAME | SYMOPT_LOAD_LINES | SYMOPT_DEFERRED_LOADS);
  SymInitialize(hProcess, "../Sources/Engine/Release;../Sources/EntitiesMP/Release;.", TRUE);
  CONTEXT ctx = *pep->ContextRecord;
  STACKFRAME64 sf;
  memset(&sf, 0, sizeof(sf));
  sf.AddrPC.Offset = ctx.Rip;    sf.AddrPC.Mode = AddrModeFlat;
  sf.AddrFrame.Offset = ctx.Rbp; sf.AddrFrame.Mode = AddrModeFlat;
  sf.AddrStack.Offset = ctx.Rsp; sf.AddrStack.Mode = AddrModeFlat;
  fprintf(stderr, "access violation, %s %p%c", pep->ExceptionRecord->ExceptionInformation[0] ? "writing" : "reading",
          (void *)pep->ExceptionRecord->ExceptionInformation[1], NLCH);
  for (INDEX i = 0; i < 24; i++) {
    if (!StackWalk64(IMAGE_FILE_MACHINE_AMD64, hProcess, GetCurrentThread(), &sf, &ctx, NULL,
                     SymFunctionTableAccess64, SymGetModuleBase64, NULL)) { break; }
    char buf[sizeof(SYMBOL_INFO) + 512];
    SYMBOL_INFO *psi = (SYMBOL_INFO *)buf;
    psi->SizeOfStruct = sizeof(SYMBOL_INFO);
    psi->MaxNameLen = 511;
    DWORD64 dwDisp = 0;
    IMAGEHLP_LINE64 line;
    line.SizeOfStruct = sizeof(line);
    DWORD dwLineDisp = 0;
    if (SymFromAddr(hProcess, sf.AddrPC.Offset, &dwDisp, psi)) {
      fprintf(stderr, "  %s+0x%llx", psi->Name, (unsigned long long)dwDisp);
      if (SymGetLineFromAddr64(hProcess, sf.AddrPC.Offset, &dwLineDisp, &line)) {
        fprintf(stderr, " %s:%lu", line.FileName, (unsigned long)line.LineNumber);
      }
      fprintf(stderr, "%c", NLCH);
    } else {
      fprintf(stderr, "  %p%c", (void *)sf.AddrPC.Offset, NLCH);
    }
  }
  fflush(stderr);
  ExitProcess(9);
  return EXCEPTION_CONTINUE_SEARCH;
}

int main(int argc, char *argv[])
{
  AddVectoredExceptionHandler(1, CrashHandler);
  int iResult = 1;
  setvbuf(stdout, NULL, _IONBF, 0);
  CTSTREAM_BEGIN {
    iResult = SubMain(argc, argv);
  } CTSTREAM_END;
  return iResult;
}
