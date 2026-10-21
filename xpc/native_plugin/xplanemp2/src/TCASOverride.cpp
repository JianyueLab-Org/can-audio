/*
 * Copyright (c) 2004, Ben Supnik and Chris Serio.
 * Copyright (c) 2018, Chris Collins.
 *
 * Permission is hereby granted, free of charge, to any person obtaining a
 * copy of this software and associated documentation files (the "Software"),
 * to deal in the Software without restriction, including without limitation
 * the rights to use, copy, modify, merge, publish, distribute, sublicense,
 * and/or sell copies of the Software, and to permit persons to whom the
 * Software is furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
 * THE SOFTWARE.
 *
 */

#include "TCASOverride.h"
#include "XPMPMultiplayerVars.h"

#include <cmath>
#include <cstdint>
#include <vector>
#include <algorithm>
#include <XPLMDataAccess.h>
#include <XPLMPlanes.h>
#include <XPLMDisplay.h>
#include <XPLMProcessing.h>
#include <XPLMUtilities.h>

/******************************************************************************

 The "TCAS hack" is no longer needed as Laminar now provide a proper TCAS API:
 https://developer.x-plane.com/article/overriding-tcas-and-providing-traffic-information/

 ******************************************************************************/

XPLMDataRef							TCAS::gOverrideRef = nullptr;
XPLMDataRef							TCAS::gXCoordRef = nullptr;
XPLMDataRef							TCAS::gYCoordRef = nullptr;
XPLMDataRef							TCAS::gZCoordRef = nullptr;
XPLMDataRef							TCAS::gHeadingRef = nullptr;
XPLMDataRef							TCAS::gModeSRef = nullptr;
XPLMDataRef							TCAS::gFlightRef = nullptr;
XPLMDataRef							TCAS::gICAOType = nullptr;
XPLMDataRef							TCAS::gWeightOnWheels = nullptr;
XPLMDataRef							TCAS::gSSRMode = nullptr;
bool								TCAS::gTCASHooksRegistered = false;
const std::size_t					TCAS::gMaxTCASItems = 63;


void
TCAS::Init()
{
	gOverrideRef = XPLMFindDataRef("sim/operation/override/override_TCAS");
	gXCoordRef = XPLMFindDataRef("sim/cockpit2/tcas/targets/position/x");
	gYCoordRef = XPLMFindDataRef("sim/cockpit2/tcas/targets/position/y");
	gZCoordRef = XPLMFindDataRef("sim/cockpit2/tcas/targets/position/z");
	gHeadingRef = XPLMFindDataRef("sim/cockpit2/tcas/targets/position/psi");
	gModeSRef = XPLMFindDataRef("sim/cockpit2/tcas/targets/modeS_id");
	gFlightRef = XPLMFindDataRef("sim/cockpit2/tcas/targets/flight_id");
	gICAOType = XPLMFindDataRef("sim/cockpit2/tcas/targets/icao_type");
	gWeightOnWheels = XPLMFindDataRef("sim/cockpit2/tcas/targets/position/weight_on_wheels");
	gSSRMode = XPLMFindDataRef("sim/cockpit2/tcas/targets/ssr_mode"); // XP12
}

void
TCAS::EnableHooks()
{
	if (!gTCASHooksRegistered) {
		XPLMSetDatai(gOverrideRef, 1);
		gTCASHooksRegistered = true;
	}
}

void
TCAS::DisableHooks()
{
	if (gTCASHooksRegistered) {
		XPLMSetDatai(gOverrideRef, 0);
		gTCASHooksRegistered = false;
	}
}

std::vector<TCAS::plane_record> TCAS::gTCASPlanes;

void
TCAS::cleanFrame()
{
	gTCASPlanes.clear();
}

void
TCAS::addPlane(float distanceSqr, float x, float y, float z, float heading, const char *name, const char *icao, bool wow, int mode, void *plane)
{
	if (!std::isnormal(distanceSqr) || !std::isnormal(x) || !std::isnormal(y) || !std::isnormal(z) || !std::isnormal(heading))
	{
		XPLMDebugString(name);
		XPLMDebugString(": non-normal TCAS data\n");
		return;
	}
	int mode_S = reinterpret_cast<std::uintptr_t>(plane) & 0xffffffu;
	gTCASPlanes.push_back({ distanceSqr, x, y, z, heading, mode_S, name, icao, wow ? 1 : 0, mode });
}

void
TCAS::pushPlanes()
{
	std::sort(gTCASPlanes.begin(), gTCASPlanes.end());
	const int count = static_cast<int>((std::min)(gMaxTCASItems, gTCASPlanes.size()));
	XPLMSetActiveAircraftCount(count + 1);
	auto plane = gTCASPlanes.begin();
	for (int i = 0; i < count; ++i, ++plane)
	{
		XPLMSetDatavf(gXCoordRef, &plane->x, i + 1, 1);
		XPLMSetDatavf(gYCoordRef, &plane->y, i + 1, 1);
		XPLMSetDatavf(gZCoordRef, &plane->z, i + 1, 1);
		XPLMSetDatavf(gHeadingRef, &plane->heading, i + 1, 1);
		XPLMSetDatavi(gModeSRef, &plane->mode_S, i + 1, 1);
		XPLMSetDatab(gFlightRef, plane->name.bytes, (i + 1) * 8, 8);
		XPLMSetDatab(gICAOType, plane->icaoType.bytes, (i + 1) * 8, 8);
		XPLMSetDatavi(gWeightOnWheels, &plane->wow, i + 1, 1);
		if (gSSRMode)
		{
			XPLMSetDatavi(gSSRMode, &plane->ssrMode, i + 1, 1);
		}
	}
}
