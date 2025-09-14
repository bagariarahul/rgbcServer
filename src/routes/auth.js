// FIXED AUTH ROUTES - Complete working version
const express = require('express');
const passport = require('passport');
const GoogleStrategy = require('passport-google-oauth20').Strategy;
const jwt = require('jsonwebtoken');
const bcrypt = require('bcryptjs');
const crypto = require('crypto');
const { body, validationResult } = require('express-validator');
const rateLimit = require('express-rate-limit');

const { User, SyncSession, Device } = require('../config/database');
const logger = require('../config/logger');
const { RedisService } = require('../config/redis');
const { authMiddleware } = require('../middleware/auth');

const router = express.Router();

// Configure Passport for Google OAuth
passport.use(new GoogleStrategy({
    clientID: process.env.GOOGLE_CLIENT_ID,
    clientSecret: process.env.GOOGLE_CLIENT_SECRET,
    callbackURL: process.env.GOOGLE_REDIRECT_URI || "/api/auth/google/callback"
}, async (accessToken, refreshToken, profile, done) => {
    try {
        logger.info('Google OAuth callback received', { 
            googleId: profile.id, 
            email: profile.emails[0].value 
        });

        // Check if user exists with this Google ID
        let user = await User.findByGoogleId(profile.id);
        
        if (user) {
            // Update refresh token if provided
            if (refreshToken && user.google_refresh_token !== refreshToken) {
                user.google_refresh_token = refreshToken;
                await user.save();
            }
            
            logger.auth('google_login_existing', user.id, { provider: 'google' });
            return done(null, user);
        }

        // Check if user exists with the same email
        user = await User.findByEmail(profile.emails[0].value);
        
        if (user) {
            // Link Google account to existing user
            await User.linkGoogleAccount(user.id, profile, refreshToken);
            await user.reload();
            
            logger.auth('google_account_linked', user.id, { provider: 'google' });
            return done(null, user);
        }

        // Create new user
        user = await User.createGoogleUser(profile, refreshToken);
        
        logger.auth('google_user_created', user.id, { provider: 'google' });
        return done(null, user);

    } catch (error) {
        logger.error('Google OAuth strategy error:', error);
        return done(error, null);
    }
}));

// Serialize user for session
passport.serializeUser((user, done) => {
    done(null, user.id);
});

passport.deserializeUser(async (id, done) => {
    try {
        const user = await User.findByPk(id);
        done(null, user);
    } catch (error) {
        done(error, null);
    }
});

// Initialize Passport
router.use(passport.initialize());
router.use(passport.session());

router.get('/test', (req, res) => {
  res.json({ ok: true, route: '/api/auth/test' });
});

// Rate limiting for auth endpoints
const authLimiter = rateLimit({
    windowMs: 15 * 60 * 1000, // 15 minutes
    max: 10, // 10 attempts per window
    message: {
        error: 'Too many authentication attempts',
        message: 'Please try again in 15 minutes',
        code: 'AUTH_RATE_LIMIT_EXCEEDED'
    },
    standardHeaders: true,
    legacyHeaders: false
});

// Validation rules
const registerValidation = [
    body('email')
        .isEmail()
        .normalizeEmail()
        .withMessage('Valid email is required'),
    body('password')
        .isLength({ min: 8 })
        .matches(/^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[@$!%*?&])[A-Za-z\d@$!%*?&]/)
        .withMessage('Password must be at least 8 characters with uppercase, lowercase, number, and special character'),
    body('firstName')
        .isLength({ min: 1, max: 100 })
        .trim()
        .escape()
        .withMessage('First name is required'),
    body('lastName')
        .isLength({ min: 1, max: 100 })
        .trim()
        .escape()
        .withMessage('Last name is required'),
    body('deviceName')
        .isLength({ min: 1, max: 100 })
        .trim()
        .escape()
        .withMessage('Device name is required'),
    body('deviceType')
        .isIn(['ANDROID', 'IOS', 'WEB'])
        .withMessage('Valid device type is required'),
    body('deviceId')
        .isUUID()
        .withMessage('Valid device ID is required')
];

const loginValidation = [
    body('email')
        .isEmail()
        .normalizeEmail()
        .withMessage('Valid email is required'),
    body('password')
        .isLength({ min: 1 })
        .withMessage('Password is required'),
    body('deviceName')
        .optional()
        .isLength({ min: 1, max: 100 })
        .trim()
        .escape(),
    body('deviceType')
        .isIn(['ANDROID', 'IOS', 'WEB'])
        .withMessage('Valid device type is required'),
    body('deviceId')
        .isUUID()
        .withMessage('Valid device ID is required')
];

// Helper function to generate JWT tokens
const generateTokens = (user, sessionId) => {
    const payload = {
        userId: user.id,
        sessionId: sessionId,
        email: user.email,
        deviceId: sessionId // Using session ID as device identifier
    };

    const accessToken = jwt.sign(
        payload,
        process.env.JWT_SECRET,
        { expiresIn: process.env.JWT_EXPIRES_IN || '7d' }
    );

    const refreshToken = jwt.sign(
        { ...payload, type: 'refresh' },
        process.env.REFRESH_TOKEN_SECRET,
        { expiresIn: process.env.REFRESH_TOKEN_EXPIRES_IN || '30d' }
    );

    return { accessToken, refreshToken };
};

// Helper function to create session - FIXED VERSION
const createSession = async (user, deviceInfo) => {
    try {
        // Find or create device
        let device = await Device.findOne({
            where: {
                user_id: user.id,
                device_id: deviceInfo.deviceId
            }
        });

        if (!device) {
            device = await Device.create({
                user_id: user.id,
                device_id: deviceInfo.deviceId,
                device_name: deviceInfo.deviceName || `${deviceInfo.deviceType} Device`,
                device_type: deviceInfo.deviceType,
                device_fingerprint: crypto.createHash('sha256')
                    .update(`${deviceInfo.deviceId}-${deviceInfo.deviceType}-${Date.now()}`)
                    .digest('hex').substring(0, 32),
                last_seen_at: new Date()
            });
        } else {
            // Update device info
            device.device_name = deviceInfo.deviceName || device.device_name;
            device.last_seen_at = new Date();
            await device.save();
        }

        // Generate tokens BEFORE creating session
        const sessionId = crypto.randomUUID(); // Generate session ID first
        const tokens = generateTokens(user, sessionId);
        
        // Hash tokens before creating session
        const accessTokenHash = await bcrypt.hash(tokens.accessToken, 5);
        const refreshTokenHash = await bcrypt.hash(tokens.refreshToken, 5);

        // Create sync session with all required fields
        const sessionData = {
            id: sessionId, // Set the ID we used for token generation
            user_id: user.id,
            device_id: device.id,
            access_token_hash: accessTokenHash, // Include from start
            refresh_token_hash: refreshTokenHash, // Include from start
            expires_at: new Date(Date.now() + (7 * 24 * 60 * 60 * 1000)),
            last_activity: new Date()
        };

        const session = await SyncSession.create(sessionData);

        return {
            session,
            device,
            tokens
        };

    } catch (error) {
        logger.error('Failed to create session:', error);
        throw error;
    }
};

// =============================================================================
// AUTHENTICATION ROUTES
// =============================================================================

// Register endpoint
router.post('/register', authLimiter, registerValidation, async (req, res) => {
    try {
        // Check validation results
        const errors = validationResult(req);
        if (!errors.isEmpty()) {
            return res.status(400).json({
                error: 'Validation failed',
                message: 'Please check your input data',
                details: errors.array()
            });
        }

        const { email, password, firstName, lastName, deviceName, deviceType, deviceId } = req.body;

        // Check if user already exists
        const existingUser = await User.findByEmail(email);
        if (existingUser) {
            logger.audit('registration_attempt_existing_email', { email }, req);
            return res.status(409).json({
                error: 'Email already registered',
                message: 'An account with this email already exists',
                code: 'EMAIL_EXISTS'
            });
        }

        // Create user
        const user = await User.create({
            email: email.toLowerCase(),
            password_hash: password,
            first_name: firstName,
            last_name: lastName
        });

        // Create session and device
        const { session, device, tokens } = await createSession(user, {
            deviceId,
            deviceName,
            deviceType
        });

        logger.auth('user_registered', user.id, {
            method: 'local',
            deviceType,
            deviceName
        }, req);

        res.status(201).json({
            message: 'User registered successfully',
            user: user.toPublicJSON(),
            tokens,
            session: {
                id: session.id,
                expiresAt: session.expires_at
            },
            device: {
                id: device.id,
                name: device.device_name,
                type: device.device_type
            }
        });

    } catch (error) {
        logger.error('Registration error:', error);
        res.status(500).json({
            error: 'Registration failed',
            message: 'An internal server error occurred',
            code: 'REGISTRATION_ERROR'
        });
    }
});

// FIXED LOGIN ENDPOINT WITH DEBUG
// EMERGENCY LOGIN FIX - Replace your login route with this:

router.post('/login', authLimiter, loginValidation, async (req, res) => {
    try {
        console.log('🔍 LOGIN DEBUG: Starting login process');
        
        // Check validation results
        const errors = validationResult(req);
        if (!errors.isEmpty()) {
            console.log('❌ LOGIN DEBUG: Validation failed:', errors.array());
            return res.status(400).json({
                error: 'Validation failed',
                message: 'Please check your input data',
                details: errors.array()
            });
        }

        const { email, password, deviceName, deviceType, deviceId } = req.body;
        
        // FIXED: Try multiple email formats to find the user
        console.log('🔍 LOGIN DEBUG: Searching for email:', email);
        
        let user = await User.findByEmail(email);
        
        if (!user) {
            // Try lowercase version
            console.log('🔍 LOGIN DEBUG: Trying lowercase email:', email.toLowerCase());
            user = await User.findByEmail(email.toLowerCase());
        }
        
        if (!user) {
            // Try normalized version manually
            const normalizedEmail = email.toLowerCase().trim();
            console.log('🔍 LOGIN DEBUG: Trying normalized email:', normalizedEmail);
            
            // Direct database search as fallback
            user = await User.findOne({
                where: {
                    email: normalizedEmail
                }
            });
        }
        
        if (!user) {
            // Last resort: search all emails to see what we have
            console.log('🔍 LOGIN DEBUG: Searching all users to debug...');
            const allUsers = await User.findAll({
                attributes: ['id', 'email', 'first_name'],
                limit: 10
            });
            console.log('🔍 LOGIN DEBUG: Recent users in database:', allUsers.map(u => ({ id: u.id, email: u.email })));
            
            logger.audit('login_attempt_invalid_email', { email }, req);
            return res.status(401).json({
                error: 'Invalid credentials',
                message: 'Email or password is incorrect',
                code: 'INVALID_CREDENTIALS'
            });
        }

        console.log('✅ LOGIN DEBUG: User found:', {
            id: user.id,
            storedEmail: user.email,
            requestEmail: email,
            hasPasswordHash: !!user.password_hash
        });

        // Check if user has password hash
        if (!user.password_hash) {
            console.log('❌ LOGIN DEBUG: No password hash - OAuth user');
            return res.status(400).json({
                error: 'OAuth user',
                message: 'This account uses Google sign-in. Please use Google authentication.',
                code: 'OAUTH_USER_LOGIN_ATTEMPT'
            });
        }

        // Simple account lock check
        if (user.locked_until && user.locked_until > new Date()) {
            console.log('🔒 LOGIN DEBUG: Account locked');
            return res.status(423).json({
                error: 'Account locked',
                message: 'Account is temporarily locked due to too many failed attempts',
                code: 'ACCOUNT_LOCKED',
                lockedUntil: user.locked_until
            });
        }

        // FIXED: Direct bcrypt password comparison
        console.log('🔍 LOGIN DEBUG: Comparing password...');
        const isPasswordValid = await bcrypt.compare(password, user.password_hash);
        console.log('🔍 LOGIN DEBUG: Password valid:', isPasswordValid);
        
        if (!isPasswordValid) {
            console.log('❌ LOGIN DEBUG: Invalid password');
            
            // Simple login attempt increment
            user.login_attempts = (user.login_attempts || 0) + 1;
            if (user.login_attempts >= 5) {
                user.locked_until = new Date(Date.now() + 15 * 60 * 1000); // 15 min lock
            }
            await user.save();
            
            logger.audit('login_attempt_invalid_password', { userId: user.id }, req);
            
            return res.status(401).json({
                error: 'Invalid credentials',
                message: 'Email or password is incorrect',
                code: 'INVALID_CREDENTIALS'
            });
        }

        console.log('✅ LOGIN DEBUG: Password validated, creating session...');

        // Reset login attempts on success
        user.login_attempts = 0;
        user.locked_until = null;
        await user.save();

        // Create session and device
        const { session, device, tokens } = await createSession(user, {
            deviceId,
            deviceName,
            deviceType
        });

        console.log('✅ LOGIN DEBUG: Session created successfully');

        logger.auth('user_logged_in', user.id, {
            method: 'local',
            deviceType,
            deviceName
        }, req);

        res.json({
            message: 'Login successful',
            user: {
                id: user.id,
                email: user.email,
                firstName: user.first_name,
                lastName: user.last_name
            },
            tokens,
            session: {
                id: session.id,
                expiresAt: session.expires_at
            },
            device: {
                id: device.id,
                name: device.device_name,
                type: device.device_type
            }
        });

    } catch (error) {
        console.log('❌ LOGIN DEBUG: Unexpected error:', error.message);
        console.log('❌ LOGIN DEBUG: Stack:', error.stack);
        logger.error('Login error:', error);
        res.status(500).json({
            error: 'Login failed',
            message: 'An internal server error occurred',
            code: 'LOGIN_ERROR'
        });
    }
});

// ADD THIS DEBUG ENDPOINT TO SEE ALL USERS:
router.get('/debug-users', async (req, res) => {
    try {
        const users = await User.findAll({
            attributes: ['id', 'email', 'first_name', 'last_name', 'createdAt'],
            limit: 20,
            order: [['createdAt', 'DESC']]
        });
        
        res.json({
            totalUsers: users.length,
            users: users.map(user => ({
                id: user.id,
                email: user.email,
                name: `${user.first_name} ${user.last_name}`,
                created: user.createdAt
            }))
        });
    } catch (error) {
        res.status(500).json({ error: error.message });
    }
});

// DEBUG PASSWORD TEST ENDPOINT
router.post('/debug-password', async (req, res) => {
    try {
        const { email, password } = req.body;
        
        const user = await User.findByEmail(email);
        if (!user) {
            return res.json({ error: 'User not found' });
        }

        // Test direct bcrypt comparison
        const isValid = await bcrypt.compare(password, user.password_hash);
        
        // Test creating a new hash with the same password
        const newHash = await bcrypt.hash(password, 12);
        const newHashTest = await bcrypt.compare(password, newHash);

        res.json({
            userFound: true,
            hasPasswordHash: !!user.password_hash,
            originalHashTest: isValid,
            newHashTest: newHashTest,
            hashPreview: user.password_hash.substring(0, 30) + '...',
            newHashPreview: newHash.substring(0, 30) + '...'
        });

    } catch (error) {
        res.status(500).json({ error: error.message });
    }
});

// Google OAuth routes
router.get('/google', 
    passport.authenticate('google', { 
        scope: ['profile', 'email'],
        prompt: 'select_account'
    })
);

router.get('/google/callback',
    passport.authenticate('google', { failureRedirect: '/auth/error' }),
    async (req, res) => {
        try {
            const user = req.user;
            
            // For OAuth flow, we need device info from query params or create default
            const deviceInfo = {
                deviceId: req.query.device_id || crypto.randomUUID(),
                deviceName: req.query.device_name || 'Web Browser',
                deviceType: req.query.device_type || 'WEB'
            };

            // Create session
            const { session, device, tokens } = await createSession(user, deviceInfo);

            logger.auth('google_oauth_success', user.id, {
                method: 'google',
                deviceType: deviceInfo.deviceType
            }, req);

            // For web clients, redirect with tokens in URL (consider security implications)
            const redirectUrl = new URL(process.env.CLIENT_URL || 'http://localhost:3000/auth/success');
            redirectUrl.searchParams.set('token', tokens.accessToken);
            redirectUrl.searchParams.set('refresh_token', tokens.refreshToken);
            
            res.redirect(redirectUrl.toString());

        } catch (error) {
            logger.error('Google OAuth callback error:', error);
            res.redirect('/auth/error');
        }
    }
);

// Token refresh endpoint
router.post('/refresh', async (req, res) => {
    try {
        const { refreshToken } = req.body;
        
        if (!refreshToken) {
            return res.status(400).json({
                error: 'Refresh token required',
                message: 'Refresh token is missing',
                code: 'REFRESH_TOKEN_MISSING'
            });
        }

        // Verify refresh token
        let decoded;
        try {
            decoded = jwt.verify(refreshToken, process.env.REFRESH_TOKEN_SECRET);
        } catch (error) {
            logger.audit('invalid_refresh_token_attempt', { error: error.message }, req);
            return res.status(401).json({
                error: 'Invalid refresh token',
                message: 'Refresh token is invalid or expired',
                code: 'INVALID_REFRESH_TOKEN'
            });
        }

        // Find session
        const session = await SyncSession.findOne({
            where: { id: decoded.sessionId },
            include: [{
                model: User,
                as: 'user'
            }]
        });

        if (!session || !session.user) {
            return res.status(401).json({
                error: 'Session not found',
                message: 'Session has expired or been revoked',
                code: 'SESSION_NOT_FOUND'
            });
        }

        // Verify stored refresh token
        const isValidRefreshToken = await bcrypt.compare(refreshToken, session.refresh_token_hash);
        if (!isValidRefreshToken) {
            logger.audit('refresh_token_mismatch', { 
                userId: session.user.id, 
                sessionId: session.id 
            }, req);
            
            return res.status(401).json({
                error: 'Invalid refresh token',
                message: 'Refresh token does not match',
                code: 'REFRESH_TOKEN_MISMATCH'
            });
        }

        // Generate new tokens
        const newTokens = generateTokens(session.user, session.id);

        // Update session with new token hashes
        session.access_token_hash = await bcrypt.hash(newTokens.accessToken, 5);
        session.refresh_token_hash = await bcrypt.hash(newTokens.refreshToken, 5);
        session.last_activity = new Date();
        await session.save();

        logger.auth('token_refreshed', session.user.id, { sessionId: session.id }, req);

        res.json({
            message: 'Tokens refreshed successfully',
            tokens: newTokens,
            expiresAt: session.expires_at
        });

    } catch (error) {
        logger.error('Token refresh error:', error);
        res.status(500).json({
            error: 'Token refresh failed',
            message: 'An internal server error occurred',
            code: 'TOKEN_REFRESH_ERROR'
        });
    }
});

// Logout endpoint
router.post('/logout', authMiddleware, async (req, res) => {
    try {
        const { sessionId } = req.user;

        // Find and delete the session
        const session = await SyncSession.findByPk(sessionId);
        if (session) {
            await session.destroy();
            logger.auth('user_logged_out', req.user.id, { sessionId }, req);
        }

        // Clear any Redis cache for this user
        await RedisService.del(`user_session:${req.user.id}:${sessionId}`);
        await RedisService.del(`user_devices:${req.user.id}`);

        res.json({
            message: 'Logout successful'
        });

    } catch (error) {
        logger.error('Logout error:', error);
        res.status(500).json({
            error: 'Logout failed',
            message: 'An internal server error occurred',
            code: 'LOGOUT_ERROR'
        });
    }
});

// Get current user info
router.get('/me', authMiddleware, async (req, res) => {
    try {
        const user = await User.findByPk(req.user.id, {
            include: [{
                model: Device,
                as: 'devices',
                where: { is_active: true },
                required: false
            }]
        });

        if (!user) {
            return res.status(404).json({
                error: 'User not found',
                message: 'User account no longer exists',
                code: 'USER_NOT_FOUND'
            });
        }

        res.json({
            user: user.toPublicJSON(),
            devices: user.devices || []
        });

    } catch (error) {
        logger.error('Get user info error:', error);
        res.status(500).json({
            error: 'Failed to get user info',
            message: 'An internal server error occurred',
            code: 'USER_INFO_ERROR'
        });
    }
});

// Revoke all sessions (logout from all devices)
router.post('/revoke-all', authMiddleware, async (req, res) => {
    try {
        // Delete all sessions for this user
        const deletedSessions = await SyncSession.destroy({
            where: { user_id: req.user.id }
        });

        // Clear Redis cache
        await RedisService.invalidatePattern(`user_session:${req.user.id}:*`);
        await RedisService.del(`user_devices:${req.user.id}`);

        logger.auth('all_sessions_revoked', req.user.id, { 
            deletedSessions 
        }, req);

        res.json({
            message: 'All sessions revoked successfully',
            revokedSessions: deletedSessions
        });

    } catch (error) {
        logger.error('Revoke all sessions error:', error);
        res.status(500).json({
            error: 'Failed to revoke sessions',
            message: 'An internal server error occurred',
            code: 'REVOKE_SESSIONS_ERROR'
        });
    }
});

module.exports = router;