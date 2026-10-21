/*
 * Copyright (c) 2018, 2020, Chris Collins.
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

#include <algorithm>
#include <cstring>

#include <XPLMGraphics.h>
#include <XPLMProcessing.h>
#include <XPLMPlanes.h>
#include <XPLMDataAccess.h>

#include "XPMPMultiplayer.h"

#include "XPMPPlane.h"
#include "PlaneType.h"
#include "Renderer.h"
#include "CullInfo.h"
#include "TCASOverride.h"
#include "CSLLibrary.h"

using namespace std;

XPMPPlane::XPMPPlane() :
	mPlaneType("", "", ""),
	mOnGround(false),
	mCSL(nullptr),
	mMatchQuality(0),
	mInstanceData(nullptr)
{
}

XPMPPlane::~XPMPPlane()
{
	setCSL(nullptr);
}

void
XPMPPlane::setType(const PlaneType &type)
{
	if (mPlaneType != type) {
		mPlaneType = type;
		setCSL(nullptr);
	}
}

void
XPMPPlane::setCSL(CSL *csl)
{
	if (mCSL != csl) {
		if (mInstanceData) {
			delete mInstanceData;
			mInstanceData = nullptr;
		}
		mCSL = csl;
	}
}

void
XPMPPlane::updatePosition(const XPMPPlanePosition_t &newPosition)
{
	memcpy(&mPosition, &newPosition, min(newPosition.size, sizeof(mPosition)));
}

void
XPMPPlane::updateSurfaces(const XPMPPlaneSurfaces_t &newSurfaces)
{
	memcpy(&mSurface, &newSurfaces, min(newSurfaces.size, sizeof(mSurface)));
}

void
XPMPPlane::updateSurveillance(const XPMPPlaneSurveillance_t &newSurveillance)
{
	memcpy(&mSurveillance, &newSurveillance, min(newSurveillance.size, sizeof(mSurveillance)));
}

void XPMPPlane::updateOnGround(bool onGround)
{
	mOnGround = onGround;
}

float
XPMPPlane::doInstanceUpdate(const CullInfo &gl_camera)
{
	if (mCSL) {
		double	lx,ly,lz;

		XPLMWorldToLocal(mPosition.lat, mPosition.lon, mPosition.elevation * kFtToMeters, &lx, &ly, &lz);
		XPLMPlaneDrawState_t planeState = {};

		planeState.structSize = sizeof(planeState);
		planeState.gearPosition = mSurface.gearPosition;
		planeState.flapRatio = mSurface.flapRatio;
		planeState.spoilerRatio = mSurface.spoilerRatio;
		planeState.speedBrakeRatio = mSurface.speedBrakeRatio;
		planeState.slatRatio = mSurface.slatRatio;
		planeState.wingSweep = mSurface.wingSweep;
		planeState.thrust = mSurface.thrust;
		planeState.yokePitch = mSurface.yokePitch;
		planeState.yokeHeading = mSurface.yokeHeading;
		planeState.yokeRoll = mSurface.yokeRoll;

        mCSL->updateInstance(
            gl_camera,
            lx,
            ly,
            lz,
            mPosition.roll,
            mPosition.heading,
            mPosition.pitch,
            mPosition.clampToGround,
            mPosition.offsetScale,
            mSurface.lights,
            mInstanceData,
            &planeState);

		if (mInstanceData == nullptr) {
			return 0.0;
		}

		int ssrMode = 0;
		switch (mSurveillance.mode)
		{
			case xpmpTransponderMode_Standby: ssrMode = 1; break;
			case xpmpTransponderMode_Mode3A: ssrMode = 2; break;
			case xpmpTransponderMode_ModeC:
			case xpmpTransponderMode_ModeC_Low:
			case xpmpTransponderMode_ModeC_Ident: ssrMode = 3; break;
		}
		// populate the global TCAS list
		TCAS::addPlane(mInstanceData->mDistanceSqr, static_cast<float>(lx), static_cast<float>(ly), static_cast<float>(lz),
			mPosition.heading, mPosition.label, mPlaneType.mICAO.c_str(), mOnGround, ssrMode, this);

		// do labels.
#if 0
		if (!mInstanceData->mCulled && mInstanceData->mDistanceSqr <= (Render_LabelDistance * Render_LabelDistance)) {
			float tx, ty;

			gl_camera.ConvertTo2D(lx, ly, lz, 1.0, &tx, &ty);
			gLabelList.emplace_back(Label{
				tx, ty,
				mInstanceData->mDistanceSqr,
				string(mPosition.label) 
			});
		}
#endif
		return mInstanceData->mDistanceSqr;
	}
	return 0.0;
}

void
XPMPPlane::setCSL(const PlaneType &type)
{
	setCSL(CSL_MatchPlane(type, &mMatchQuality, true));
}

void
XPMPPlane::updateCSL()
{
	setCSL(mPlaneType);
}

bool
XPMPPlane::upgradeCSL(const PlaneType &type)
{
	int local_matchquality;
	auto newCSL = CSL_MatchPlane(type, &local_matchquality, false);
	if (local_matchquality >= 0 && local_matchquality < mMatchQuality) {
		setCSL(newCSL);
		mMatchQuality = local_matchquality;
		return true;
	}
	return false;
}

int
XPMPPlane::getMatchQuality()
{
	return mMatchQuality;
}
