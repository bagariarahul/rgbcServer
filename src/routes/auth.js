const express = require('express');
const router = express.Router();
const { body, validationResult } = require('express-validator');
const jwt = require('jsonwebtoken');
const bcrypt = require('bcryptjs');
const crypto = require('crypto'); // Required for ID generation and hashing
const logger = require('../config/logger');

const { OAuth2Client } = require('google-auth-library');
const googleClient = new OAuth2Client(process.env.GOOGLE_CLIENT_ID);

// Safe Import
const { User, Device, SyncSession } = require('../config/database');

// --- Helper Functions ---
function generateTokens(user, sessionId) {
    const payload = {
        userId: user.id,
        email: user.email,
        sessionId: sessionId,
        iat: Math.floor(Date.now() / 1000)
    };

    const accessToken = jwt.sign(payload, process.env.JWT_SECRET || 'demo_secret', {
        expiresIn: '1h'
    });

    const refreshToken = jwt.sign(
        { userId: user.id, sessionId, type: 'refresh' },
        process.env.JWT_REFRESH_SECRET || process.env.JWT_SECRET || 'demo_secret',
        { expiresIn: '7d' }
    );

    return { accessToken, refreshToken };
}

async function createSession(user, deviceId, deviceName, deviceType) {
    try {
        // 1. Handle Device
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

        // 2. Pre-generate Session ID and Tokens
        // We do this BEFORE DB insertion so we can hash the tokens for storage
        const sessionId = crypto.randomUUID(); 
        const tokens = generateTokens(user, sessionId);

        // 3. Hash Tokens for Database Storage
        const accessTokenHash = crypto.createHash('sha256').update(tokens.accessToken).digest('hex');
        const refreshTokenHash = crypto.createHash('sha256').update(tokens.refreshToken).digest('hex');

        // 4. Create Session with Hashes
        let session = null;
        if (SyncSession) {
            session = await SyncSession.create({
                id: sessionId, // Use our pre-generated ID
                user_id: user.id,
                device_id: device ? device.id : null,
                access_token_hash: accessTokenHash,  // FIX: Saving the hash
                refresh_token_hash: refreshTokenHash, // FIX: Saving the hash
                is_active: true,
                expires_at: new Date(Date.now() + (7 * 24 * 60 * 60 * 1000))
            });
        }

        return { session, device, tokens };
    } catch (error) {
        logger.error('Session creation failed:', error);
        throw error;
    }
}

// --- Validation ---
const registerValidation = [
    body('email').isEmail().normalizeEmail(),
    body('password').isLength({ min: 6 }),
    body('firstName').trim().notEmpty(),
    body('lastName').trim().notEmpty()
];

const loginValidation = [
    body('email').isEmail().normalizeEmail(),
    body('password').exists()
];

// --- Routes ---

// POST /api/auth/register
router.post('/register', registerValidation, async (req, res) => {
    const errors = validationResult(req);
    if (!errors.isEmpty()) return res.status(400).json({ errors: errors.array() });

    const { email, password, firstName, lastName, deviceId, deviceName, deviceType } = req.body;

    try {
        const existingUser = await User.findByEmail(email);
        if (existingUser) {
            return res.status(409).json({ message: 'Email already registered' });
        }

        // Create User
        const user = await User.create({
            email,
            password_hash: password, 
            first_name: firstName,
            last_name: lastName,
            auth_provider: 'LOCAL'
        });

        // Create Session & Tokens
        const { tokens } = await createSession(user, deviceId || 'unknown', deviceName || 'Generic', deviceType || 'WEB');

        logger.info(`New user registered: ${email}`);
        
        res.status(201).json({
            message: 'User registered',
            user: user.toPublicJSON(),
            tokens
        });

    } catch (error) {
        logger.error('Register Error:', error);
        res.status(500).json({ message: 'Server error during registration' });
    }
});

// POST /api/auth/login
router.post('/login', loginValidation, async (req, res) => {
    const errors = validationResult(req);
    if (!errors.isEmpty()) return res.status(400).json({ errors: errors.array() });

    const { email, password, deviceId, deviceName, deviceType } = req.body;

    try {
        const user = await User.findByEmail(email);
        if (!user) return res.status(400).json({ message: 'Invalid credentials' });

        if (user.isLocked()) {
            return res.status(403).json({ message: `Account locked until ${user.locked_until}` });
        }

        const isMatch = await user.validatePassword(password);
        if (!isMatch) {
            await user.incrementLoginAttempts();
            return res.status(400).json({ message: 'Invalid credentials' });
        }

        await user.resetLoginAttempts();
        const { tokens } = await createSession(user, deviceId || 'unknown', deviceName || 'Generic', deviceType || 'WEB');

        logger.info(`User logged in: ${email}`);
        res.json({
            message: 'Login successful',
            user: user.toPublicJSON(),
            tokens
        });

    } catch (error) {
        logger.error('Login Error:', error);
        res.status(500).json({ message: 'Server error during login' });
    }
});

// POST /api/auth/google
router.post('/google', async (req, res) => {
    const { idToken, deviceId, deviceName, deviceType } = req.body;

    if (!idToken) return res.status(400).json({ message: 'No Google ID token provided' });

    try {
        // 1. Verify token with Google's servers
        const ticket = await googleClient.verifyIdToken({
            idToken,
            audience: process.env.GOOGLE_CLIENT_ID,
        });
        const payload = ticket.getPayload();
        const email = payload.email;

        // 2. DevSecOps Check: Is this the Allowed Admin?
        if (email !== process.env.ALLOWED_ADMIN_EMAIL) {
            logger.warn(`🚨 Unauthorized Google login attempt blocked from: ${email}`);
            return res.status(403).json({ message: 'Unauthorized email address.' });
        }

        // 3. Find the user (or create the Admin user if this is the first run)
        let user = await User.findByEmail(email);
        if (!user) {
            user = await User.create({
                email,
                password_hash: crypto.randomBytes(16).toString('hex'), // Dummy password for OAuth users
                first_name: payload.given_name || 'Admin',
                last_name: payload.family_name || '',
                auth_provider: 'GOOGLE'
            });
            logger.info(`Admin user created via Google Auth: ${email}`);
        }

        // 4. Generate standard session/tokens using your existing robust function
        const { tokens } = await createSession(
            user,
            deviceId || 'unknown',
            deviceName || 'Google Auth Device',
            deviceType || 'ANDROID'
        );

        logger.info(`Admin logged in via Google: ${email}`);
        res.json({
            message: 'Google Login successful',
            user: user.toPublicJSON(),
            tokens // This returns accessToken and refreshToken matching your Android app's expectations
        });

    } catch (error) {
        logger.error('Google Auth Error:', error);
        res.status(401).json({ message: 'Invalid Google token' });
    }
});

module.exports = router;