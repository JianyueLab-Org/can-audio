/*
 * Copyright (c) 2013, Laminar Research.
 * Copyright (c) 2018,2020, Christopher Collins.
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

#include <string>
#include <cstring>
#include <XPLMDataAccess.h>
#include <XPLMScenery.h>

#include "CSL.h"
#include "XPMPMultiplayerVars.h"
#include "Renderer.h"
#include "TCASOverride.h"

using namespace std;

CSL::CSL()
{
	mOffsetSource = VerticalOffsetSource::None;
	mMovingGear = true;
}

CSL::CSL(std::vector<std::string> dirNames) :
	mDirNames(std::move(dirNames))
{
	mMovingGear = true;
	mOffsetSource = VerticalOffsetSource::None;
}

double
CSL::getVertOffset() const
{
	switch (mOffsetSource) {
	case VerticalOffsetSource::None:
		return 0.0;
	case VerticalOffsetSource::Model:
		return mModelVertOffset;
	case VerticalOffsetSource::Mtl:
		return mMtlVertOffset;
	case VerticalOffsetSource::Preference:
		return mPreferencesVertOffset;
	}
	return 0.0;
}

VerticalOffsetSource
CSL::getVertOffsetSource() const
{
	return mOffsetSource;
}

void
CSL::setVertOffsetSource(VerticalOffsetSource offsetSource)
{
	mOffsetSource = offsetSource;
}

void
CSL::setVerticalOffset(VerticalOffsetSource src, double offset)
{
	switch(src) {
	case VerticalOffsetSource::None:
		break;
	case VerticalOffsetSource::Model:
		mModelVertOffset = offset;
		break;
	case VerticalOffsetSource::Mtl:
		mMtlVertOffset = offset;
		break;
	case VerticalOffsetSource::Preference:
		mPreferencesVertOffset = offset;
		break;
	}
	if (src > mOffsetSource) {
		mOffsetSource = src;
	}
}

void
CSL::setMovingGear(bool movingGear)
{
	mMovingGear = movingGear;
}

bool
CSL::getMovingGear() const
{
	return mMovingGear;
}

void
CSL::setICAO(const std::string &icaoCode)
{
	mICAO = icaoCode;
}

void
CSL::setAirline(const std::string &icaoCode, const std::string &airline)
{
	setICAO(icaoCode);
	mAirline = airline;
}

void
CSL::setLivery(const std::string &icaoCode, const std::string &airline, const std::string &livery)
{
	setAirline(icaoCode, airline);
	mLivery = livery;
}

const std::string&
CSL::getICAO() const {
	return mICAO;
}

const std::string&
CSL::getAirline() const {
	return mAirline;
}

const std::string&
CSL::getLivery() const {
	return mLivery;
}

bool
CSL::isUsable() const {
	return true;
}

void
CSL::drawPlane(CSLInstanceData * /*instanceData*/, bool /*is_blend*/, int /*data*/) const
{
}

void
CSL::updateInstance(const CullInfo &cullInfo,
                    double &x,
                    double &y,
                    double &z,
                    double roll,
                    double heading,
                    double pitch,
                    bool clampToSurface,
                    float offsetScale,
                    xpmp_LightStatus lights,
                    CSLInstanceData *&instanceData,
                    XPLMPlaneDrawState_t *state)
{
	if (instanceData == nullptr) {
		newInstanceData(instanceData);
	}
	if (instanceData == nullptr) {
		return;
	}

	if (offsetScale >= 0.0) {
	    y += offsetScale*getVertOffset();
	}

	// clamp to the surface if enabled
	if (gConfiguration.enableSurfaceClamping && clampToSurface) {
		XPLMProbeInfo_t	probeResult = {
			sizeof(XPLMProbeInfo_t),
		};
		XPLMProbeResult r = XPLMProbeTerrainXYZ(gTerrainProbe,
			static_cast<float>(x), static_cast<float>(y), static_cast<float>(z), &probeResult);
		if (r == xplm_ProbeHitTerrain) {
			double minY = probeResult.locationY + getVertOffset();
			if (y < minY) {
				y = minY;
				instanceData->mClamped = true;
			} else {
				instanceData->mClamped = false;
			}
		}
	} else {
	    instanceData->mClamped = false;
	}

	instanceData->mDistanceSqr = cullInfo.SphereDistanceSqr(
		static_cast<float>(x), static_cast<float>(y), static_cast<float>(z));

	// we need to assess cull state so we can work out if we need to render labels or not
	instanceData->mCulled = false;
	// cull if the aircraft is not visible due to poor horizontal visibility
	if (gVisDataRef) {
		float horizVis = XPLMGetDataf(gVisDataRef);
		if (instanceData->mDistanceSqr > horizVis*horizVis) {
			instanceData->mCulled = true;
		}
	}
	instanceData->updateInstance(this, x, y, z, pitch, roll, heading, lights, state);
}