const express = require('express');
const router = express.Router();
const jwt = require('jsonwebtoken');
const crypto = require('crypto');
const logger = require('../config/logger');
const { User, Device, SyncSession } = require('../config/database');

// ── Google ID Token verification ────────────────────────────────────────
// We verify the token by calling Google's tokeninfo endpoint.
// This avoids adding google-auth-library as a dependency.
// For production, you can swap this with the google-auth-library's
// OAuth2Client.verifyIdToken() for offline verification.
async function verifyGoogleIdToken(idToken) {
    const response = await fetch(
        `https://oauth2.googleapis.com/tokeninfo?id_token=${encodeURIComponent(idToken)}`
    );

    if (!response.ok) {
        throw new Error(`Google token verification failed: ${response.status}`);
    }

    const payload = await response.json();

    // Verify the audience matches our client ID
    const expectedClientId = process.env.GOOGLE_CLIENT_ID;
    if (expectedClientId && payload.aud !== expectedClientId) {
        throw new Error('Token audience mismatch');
    }

    return {
        email: payload.email,
        emailVerified: payload.email_verified === 'true',
        name: payload.name || '',
        givenName: payload.given_name || '',
        familyName: payload.family_name || '',
        picture: payload.picture || '',
        sub: payload.sub // Google's unique user ID
    };
}

// ── Helper: Generate JWT pair ───────────────────────────────────────────
function generateTokens(user, sessionId) {
    const payload = {
        userId: user.id,
        email: user.email,
        sessionId: sessionId,
        iat: Math.floor(Date.now() / 1000)
    };

    const accessToken = jwt.sign(payload, process.env.JWT_SECRET, {
        expiresIn: '7d'  // Longer expiry for personal server
    });

    const refreshToken = jwt.sign(
        { userId: user.id, sessionId, type: 'refresh' },
        process.env.REFRESH_TOKEN_SECRET || process.env.JWT_SECRET,
        { expiresIn: '30d' }
    );

    return { accessToken, refreshToken, expiresIn: '7d' };
}

// ═══════════════════════════════════════════════════════════════════════
// POST /api/auth/google
//
// Body: { idToken: "...", deviceName: "Pixel 8", deviceType: "ANDROID", deviceId: "..." }
//
// Flow:
// 1. Verify Google ID token with Google's API
// 2. Check email against ALLOWED_ADMIN_EMAIL env var
// 3. Find-or-create user in Postgres
// 4. Create session + issue JWT
// ═══════════════════════════════════════════════════════════════════════
router.post('/google', async (req, res) => {
    try {
        const { idToken, deviceName, deviceType, deviceId } = req.body;

        if (!idToken) {
            return res.status(400).json({
                message: 'Missing idToken in request body'
            });
        }

        // 1. Verify the Google ID token
        let googleUser;
        try {
            googleUser = await verifyGoogleIdToken(idToken);
        } catch (error) {
            logger.warn('Google token verification failed:', error.message);
            return res.status(401).json({
                message: 'Invalid Google ID token',
                error: error.message
            });
        }

        if (!googleUser.emailVerified) {
            return res.status(401).json({
                message: 'Google email not verified'
            });
        }

        // 2. Admin email gate — only YOUR email can use this server
        const allowedEmail = process.env.ALLOWED_ADMIN_EMAIL;
        if (allowedEmail && googleUser.email.toLowerCase() !== allowedEmail.toLowerCase()) {
            logger.warn(`🚫 Unauthorized Google login attempt: ${googleUser.email}`);
            return res.status(403).json({
                message: 'Access denied. This server is restricted to the admin account.'
            });
        }

        // 3. Find or create user
        let user = await User.findOne({ where: { email: googleUser.email } });

        if (!user) {
            user = await User.create({
                email: googleUser.email,
                password_hash: crypto.randomBytes(32).toString('hex'), // Random — Google users don't use passwords
                first_name: googleUser.givenName || 'Admin',
                last_name: googleUser.familyName || '',
                auth_provider: 'GOOGLE',
                google_id: googleUser.sub
            });
            logger.info(`New Google user created: ${googleUser.email}`);
        }

        // 4. Handle device
        let device = null;
        if (Device && deviceId) {
            device = await Device.findOne({
                where: { user_id: user.id, device_id: deviceId }
            });

            if (!device) {
                device = await Device.create({
                    user_id: user.id,
                    device_id: deviceId,
                    device_name: deviceName || 'Android Device',
                    device_type: deviceType || 'ANDROID',
                    is_active: true,
                    last_seen: new Date()
                });
            } else {
                await device.update({ last_seen: new Date(), is_active: true });
            }
        }

        // 5. Create session + tokens
        const sessionId = crypto.randomUUID();
        const tokens = generateTokens(user, sessionId);

        if (SyncSession) {
            const accessTokenHash = crypto.createHash('sha256')
                .update(tokens.accessToken).digest('hex');
            const refreshTokenHash = crypto.createHash('sha256')
                .update(tokens.refreshToken).digest('hex');

            await SyncSession.create({
                id: sessionId,
                user_id: user.id,
                device_id: device ? device.id : null,
                access_token_hash: accessTokenHash,
                refresh_token_hash: refreshTokenHash,
                is_active: true,
                expires_at: new Date(Date.now() + (30 * 24 * 60 * 60 * 1000))
            });
        }

        logger.info(`✅ Google login successful: ${googleUser.email}`);

        res.json({
            message: 'Login successful',
            user: {
                id: user.id.toString(),
                email: user.email,
                firstName: user.first_name,
                lastName: user.last_name,
                storageQuota: '107374182400',
                storageUsed: '0',
                createdAt: user.created_at?.toISOString() || ''
            },
            tokens: {
                accessToken: tokens.accessToken,
                refreshToken: tokens.refreshToken,
                expiresIn: tokens.expiresIn
            }
        });

    } catch (error) {
        logger.error('Google auth error:', error);
        res.status(500).json({
            message: 'Server error during Google authentication'
        });
    }
});

module.exports = router;