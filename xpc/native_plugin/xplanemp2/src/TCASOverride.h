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

#ifndef XPMP_TCASHACK_H
#define XPMP_TCASHACK_H

#include <vector>
#include <cstring>

#include <XPLMDataAccess.h>
#include <XPLMDisplay.h>


class TCAS {
private:
	static XPLMDataRef						gOverrideRef;
	static XPLMDataRef						gXCoordRef;
	static XPLMDataRef						gYCoordRef;
	static XPLMDataRef						gZCoordRef;
	static XPLMDataRef						gHeadingRef;
	static XPLMDataRef						gModeSRef;
	static XPLMDataRef						gFlightRef;
	static XPLMDataRef						gICAOType;
	static XPLMDataRef						gWeightOnWheels;
	static XPLMDataRef						gSSRMode;

	static bool								gTCASHooksRegistered;

	struct plane_record {
		float distanceSqr;
		float x;
		float y;
		float z;
		float heading;
		int mode_S;
		struct name
		{
			name(const char *i_bytes) { std::strncpy(bytes, i_bytes, 7); }
			char bytes[8]{};
		} name, icaoType;
		int wow;
		int ssrMode;

		friend bool operator<(const plane_record &a, const plane_record &b)
		{
			return a.distanceSqr < b.distanceSqr;
		}
	};

	static std::vector<plane_record>		gTCASPlanes;
	static const std::size_t				gMaxTCASItems;

public:
	static void Init();
	static void EnableHooks();
	static void DisableHooks();

	static void cleanFrame();

	/** adds a plane to the list of aircraft we're going to report on */
	static void addPlane(float distanceSqr, float x, float y, float z, float heading, const char *name, const char *icao, bool wow, int mode, void *plane);

	/** forwards the list of aircraft to x-plane */
	static void pushPlanes();
};

#endif //XPMP_TCASHACK_H
