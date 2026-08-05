const express = require('express');
const router = express.Router();
const { body, validationResult } = require('express-validator');
const jwt = require('jsonwebtoken');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const logger = require('../config/logger');

const { OAuth2Client } = require('google-auth-library');
const googleClient = new OAuth2Client(process.env.GOOGLE_CLIENT_ID);

// Sprint 3: Build an array of ALL valid Google Client IDs.
// id_tokens minted by the Desktop OAuth flow have a DIFFERENT audience
// than those from the Android Credential Manager. Both are legitimate.
const VALID_CLIENT_IDS = [
    process.env.GOOGLE_CLIENT_ID,           // Android / Web
    process.env.GOOGLE_DESKTOP_CLIENT_ID,   // Python Desktop app
].filter(Boolean);

const { User, Device, SyncSession } = require('../config/database');

// ═══════════════════════════════════════════════════════════════════════
// Sprint 3 — RS256 Asymmetric JWT Signing
//
// The Gateway holds the RSA PRIVATE key and signs tokens.
// The Master Node and any future service hold only the PUBLIC key
// and can verify but NEVER forge tokens.
// ═══════════════════════════════════════════════════════════════════════


// For local 
// const KEYS_DIR = path.resolve(__dirname, '..', 'keys');

// let PRIVATE_KEY = null;
// try {
//     PRIVATE_KEY = fs.readFileSync(path.join(KEYS_DIR, 'private.pem'), 'utf8');
//     logger.info('✅ RS256 private key loaded from src/keys/private.pem');
// } catch (e) {
//     logger.error(
//         '❌ RS256 private key not found at src/keys/private.pem\n' +
//         '   Generate it with:\n' +
//         '     cd src/keys && openssl genrsa -out private.pem 2048\n' +
//         '     openssl rsa -in private.pem -pubout -out public.pem\n' +
//         '   Then restart the server.'
//     );
// }

const KEYS_DIR = path.resolve(__dirname, '..', 'keys');

// Prefer the base64 env var (Render/production — keys are NOT in Git),
// fall back to the file on disk (local dev). Same keypair either way.
let PRIVATE_KEY = null;
try {
    if (process.env.JWT_PRIVATE_KEY_B64) {
        PRIVATE_KEY = Buffer.from(process.env.JWT_PRIVATE_KEY_B64, 'base64').toString('utf8');
        logger.info('✅ RS256 private key loaded from JWT_PRIVATE_KEY_B64 env var');
    } else {
        PRIVATE_KEY = fs.readFileSync(path.join(KEYS_DIR, 'private.pem'), 'utf8');
        logger.info('✅ RS256 private key loaded from src/keys/private.pem');
    }
} catch (e) {
    logger.error(
        '❌ RS256 private key not available.\n' +
        '   Set JWT_PRIVATE_KEY_B64 (base64 of private.pem) in the environment,\n' +
        '   or place src/keys/private.pem for local dev.'
    );
}

// ── Token Generation (RS256, 30-day expiry) ─────────────────────────

function generateTokens(user, sessionId) {
    if (!PRIVATE_KEY) {
        throw new Error('RS256 private key not loaded. Cannot sign tokens.');
    }

    const payload = {
        userId: user.id,
        email: user.email,
        sessionId: sessionId,
        iat: Math.floor(Date.now() / 1000)
    };

    const accessToken = jwt.sign(payload, PRIVATE_KEY, {
        algorithm: 'RS256',
        expiresIn: '30d'
    });

    const refreshToken = jwt.sign(
        { userId: user.id, sessionId, type: 'refresh' },
        PRIVATE_KEY,
        { algorithm: 'RS256', expiresIn: '90d' }
    );

    return { accessToken, refreshToken };
}

// ── Session Creation ────────────────────────────────────────────────

async function createSession(user, deviceId, deviceName, deviceType) {
    let device = null;
    if (Device) {
        device = await Device.findOne({ where: { user_id: user.id, device_id: deviceId } });
        if (!device) {
            device = await Device.create({
                user_id: user.id,
                device_id: deviceId,
                device_name: deviceName,
                device_type: deviceType,
                is_active: true,
                last_seen: new Date()
            });
        } else {
            await device.update({ last_seen: new Date(), is_active: true });
        }
    }

    const sessionId = crypto.randomUUID();
    const tokens = generateTokens(user, sessionId);

    const accessTokenHash = crypto.createHash('sha256').update(tokens.accessToken).digest('hex');
    const refreshTokenHash = crypto.createHash('sha256').update(tokens.refreshToken).digest('hex');

    if (SyncSession) {
        await SyncSession.create({
            id: sessionId,
            user_id: user.id,
            device_id: device ? device.id : null,
            access_token_hash: accessTokenHash,
            refresh_token_hash: refreshTokenHash,
            expires_at: new Date(Date.now() + 30 * 24 * 60 * 60 * 1000),
            is_active: true
        });
    }

    return {
        sessionId,
        tokens,
        deviceId: device ? device.id : null
    };
}

// ── Helper: split Google "name" into first / last sanely ────────────
// Prefers id_token's given_name/family_name (set by Google directly),
// falls back to splitting "name", with email-prefix as last resort.
// All outputs guaranteed non-empty (matches User model's notEmpty rule).
function splitName(payload) {
    const given = (payload.given_name || '').trim();
    const family = (payload.family_name || '').trim();

    if (given || family) {
        return {
            firstName: given || family || 'User',
            lastName: family || given || ' ',  // single space avoids notEmpty failure
        };
    }

    const fullName = (payload.name || payload.email.split('@')[0]).trim();
    const parts = fullName.split(/\s+/);
    return {
        firstName: parts[0] || 'User',
        lastName: parts.slice(1).join(' ') || ' ',
    };
}

// ═══════════════════════════════════════════════════════════════════════
// POST /api/auth/google-signin
//
// Accepts a Google ID token, verifies it against ALL configured client
// IDs (Web/Android/Desktop), creates or updates the user, issues a
// 30-day RS256 JWT.
// ═══════════════════════════════════════════════════════════════════════

router.post('/google-signin', [
    body('idToken').notEmpty().withMessage('Google ID token is required'),
    body('deviceId').optional().isString(),
    body('deviceName').optional().isString(),
    body('deviceType').optional().isIn(['ANDROID', 'IOS', 'WEB', 'DESKTOP']),
], async (req, res) => {
    const errors = validationResult(req);
    if (!errors.isEmpty()) {
        return res.status(400).json({ errors: errors.array() });
    }

    try {
        const { idToken, deviceId, deviceName, deviceType } = req.body;

        // ── 1. Verify Google ID token against all valid audiences ───
        let ticket;
        try {
            ticket = await googleClient.verifyIdToken({
                idToken: idToken,
                audience: VALID_CLIENT_IDS,
            });
        } catch (verifyErr) {
            logger.warn(`Google id_token verification failed: ${verifyErr.message}`);
            return res.status(401).json({
                error: 'Invalid Google token',
                message: 'The Google ID token could not be verified.'
            });
        }

        const googlePayload = ticket.getPayload();
        const email = googlePayload.email;
        const googleId = googlePayload.sub;
        const picture = googlePayload.picture || null;
        const { firstName, lastName } = splitName(googlePayload);

        // ── 2. Sprint 3.1: Multi-tenant. Any verified Google account
        //       that completes consent is permitted to register. The
        //       ALLOWED_ADMIN_EMAIL gate is intentionally removed.
        //       Per-resource authorization happens downstream.

        // ── 3. Find or create user (matches User.js model exactly) ──
        // password_hash: random 32-byte hex placeholder for OAuth-only
        //   users. Column is nullable, but a placeholder makes accidental
        //   password-login attempts on this account fail at bcrypt compare
        //   rather than at DB lookup.
        const [user, created] = await User.findOrCreate({
            where: { email: email.toLowerCase() },
            defaults: {
                email: email.toLowerCase(),
                first_name: firstName,
                last_name: lastName,
                avatar_url: picture,
                google_id: googleId,
                auth_provider: 'GOOGLE',
                email_verified: true,
                password_hash: crypto.randomBytes(32).toString('hex'),
                is_active: true,
            }
        });

        if (created) {
            logger.info(`👤 New Google user: ${email} (id=${user.id})`);
        } else {
            // Backfill google_id/avatar for users who originally signed
            // up with a different provider (LOCAL → HYBRID).
            const updates = {};
            if (!user.google_id) updates.google_id = googleId;
            if (picture && user.avatar_url !== picture) updates.avatar_url = picture;
            if (user.auth_provider === 'LOCAL') updates.auth_provider = 'HYBRID';
            if (Object.keys(updates).length > 0) {
                await user.update(updates);
                logger.info(`🔗 Updated existing user ${email}: ${Object.keys(updates).join(', ')}`);
            }
        }

        // ── 4. Create session and issue RS256 tokens ────────────────
        const session = await createSession(
            user,
            deviceId || `web_${crypto.randomUUID().substring(0, 8)}`,
            deviceName || 'Web Browser',
            deviceType || 'WEB'
        );

        logger.info(`✅ Google login: ${email} (RS256, 30d, device=${deviceType || 'WEB'})`);

        // ── 5. Response shape ───────────────────────────────────────
        // Sprint 3.1: expiresIn is now a NUMBER (seconds), matching the
        // OAuth 2.0 RFC. The Python desktop's master_oauth.py expects
        // an int it can add to time.time(). Android already coerces.
        res.json({
            message: 'Login successful',
            user: {
                id: user.id,
                email: user.email,
                firstName: user.first_name,
                lastName: user.last_name,
                avatarUrl: user.avatar_url,
                storageQuota: String(user.storage_quota || 107374182400),
                storageUsed: String(user.storage_used || 0),
                createdAt: user.created_at ? user.created_at.toISOString() : new Date().toISOString(),
            },
            tokens: {
                accessToken: session.tokens.accessToken,
                refreshToken: session.tokens.refreshToken,
                expiresIn: 30 * 24 * 60 * 60,  // 2592000 seconds — INT, not String
            },
        });

    } catch (error) {
        logger.error('Google sign-in error:', {
            message: error.message,
            name: error.name,
            stack: error.stack,
        });

        if (error.name === 'SequelizeValidationError') {
            return res.status(400).json({
                error: 'Validation failed',
                message: error.errors.map(e => `${e.path}: ${e.message}`).join('; ')
            });
        }

        res.status(500).json({
            error: 'Authentication failed',
            message: 'Server error during sign-in. Check gateway logs.'
        });
    }
});

module.exports = router;