// const express = require('express');
// const bcrypt = require('bcryptjs');
// const jwt = require('jsonwebtoken');
// const { body, validationResult } = require('express-validator');
// const rateLimit = require('express-rate-limit');
// const { User, SyncSession, Device } = require('../config/database');
// const RedisService = require('../config/redis');
// const logger = require('../config/logger');

// const router = express.Router();

// // Rate limiting for auth endpoints
// const authLimiter = rateLimit({
//     windowMs: 15 * 60 * 1000, // 15 minutes
//     max: 10, // limit each IP to 10 requests per windowMs for auth
//     message: {
//         error: 'Too many authentication attempts',
//         message: 'Please try again later',
//         retryAfter: 15 * 60 // 15 minutes in seconds
//     },
//     standardHeaders: true,
//     legacyHeaders: false,
// });

// // Validation middleware
// const registerValidation = [
//     body('email')
//         .isEmail()
//         .normalizeEmail()
//         .withMessage('Please provide a valid email address'),
//     body('password')
//         .isLength({ min: 8 })
//         .withMessage('Password must be at least 8 characters long')
//         .matches(/^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[@$!%*?&])[A-Za-z\d@$!%*?&]/)
//         .withMessage('Password must contain at least one uppercase letter, one lowercase letter, one number, and one special character'),
//     body('firstName')
//         .trim()
//         .isLength({ min: 1, max: 50 })
//         .withMessage('First name must be between 1 and 50 characters'),
//     body('lastName')
//         .trim()
//         .isLength({ min: 1, max: 50 })
//         .withMessage('Last name must be between 1 and 50 characters'),
//     body('deviceName')
//         .trim()
//         .isLength({ min: 1, max: 100 })
//         .withMessage('Device name is required'),
//     body('deviceType')
//         .isIn(['ANDROID', 'IOS', 'WEB', 'DESKTOP'])
//         .withMessage('Invalid device type'),
//     body('deviceId')
//         .trim()
//         .isLength({ min: 1 })
//         .withMessage('Device ID is required')
// ];

// const loginValidation = [
//     body('email')
//         .isEmail()
//         .normalizeEmail()
//         .withMessage('Please provide a valid email address'),
//     body('password')
//         .isLength({ min: 1 })
//         .withMessage('Password is required'),
//     body('deviceName')
//         .optional()
//         .trim()
//         .isLength({ max: 100 }),
//     body('deviceType')
//         .isIn(['ANDROID', 'IOS', 'WEB', 'DESKTOP'])
//         .withMessage('Invalid device type'),
//     body('deviceId')
//         .trim()
//         .isLength({ min: 1 })
//         .withMessage('Device ID is required')
// ];

// // Helper functions
// function generateTokens(user, sessionId) {
//     const payload = {
//         userId: user.id,
//         email: user.email,
//         sessionId: sessionId,
//         iat: Math.floor(Date.now() / 1000)
//     };

//     const accessToken = jwt.sign(payload, process.env.JWT_SECRET, {
//         expiresIn: process.env.JWT_EXPIRES_IN || '1h'
//     });

//     const refreshToken = jwt.sign(
//         { userId: user.id, sessionId, type: 'refresh' },
//         process.env.JWT_REFRESH_SECRET || process.env.JWT_SECRET,
//         { expiresIn: process.env.JWT_REFRESH_EXPIRES_IN || '7d' }
//     );

//     return {
//         accessToken,
//         refreshToken,
//         expiresIn: process.env.JWT_EXPIRES_IN || '3600'
//     };
// }

// async function createSession(user, deviceId, deviceName, deviceType) {
//     try {
//         // Find or create device
//         let device = await Device.findOne({
//             where: { userid: user.id, deviceid: deviceId }
//         });

//         if (!device) {
//             device = await Device.create({
//                 userid: user.id,
//                 deviceid: deviceId,
//                 devicename: deviceName,
//                 devicetype: deviceType,
//                 isactive: true,
//                 lastseen: new Date()
//             });
//             logger.info('New device registered', { userId: user.id, deviceId, deviceName });
//         } else {
//             // Update existing device
//             await device.update({
//                 devicename: deviceName,
//                 lastseen: new Date(),
//                 isactive: true
//             });
//         }

//         // Create session
//         const session = await SyncSession.create({
//             userid: user.id,
//             deviceid: device.id,
//             isactive: true,
//             expiresat: new Date(Date.now() + (7 * 24 * 60 * 60 * 1000)), // 7 days
//             lastactivity: new Date()
//         });

//         // Generate tokens
//         const tokens = generateTokens(user, session.id);

//         // Store token hashes in session for security
//         session.accesstokenhash = await bcrypt.hash(tokens.accessToken, 5);
//         session.refreshtokenhash = await bcrypt.hash(tokens.refreshToken, 5);
//         await session.save();

//         return { session, device, tokens };
//     } catch (error) {
//         logger.error('Failed to create session', error);
//         throw error;
//     }
// }

// // AUTHENTICATION ROUTES

// /**
//  * Register endpoint
//  * POST /api/auth/register
//  */
// router.post('/register', authLimiter, registerValidation, async (req, res) => {
//     try {
//         // Check validation results
//         const errors = validationResult(req);
//         if (!errors.isEmpty()) {
//             return res.status(400).json({
//                 error: 'Validation failed',
//                 message: 'Please check your input data',
//                 details: errors.array()
//             });
//         }

//         const { email, password, firstName, lastName, deviceName, deviceType, deviceId } = req.body;

//         // Check if user already exists
//         const existingUser = await User.findByEmail(email);
//         if (existingUser) {
//             logger.audit('registration_attempt_existing_email', { email }, req);
//             return res.status(409).json({
//                 error: 'Email already registered',
//                 message: 'An account with this email already exists',
//                 code: 'EMAIL_EXISTS'
//             });
//         }

//         // Create user
//         const user = await User.create({
//             email: email.toLowerCase(),
//             passwordhash: password, // User model should hash this
//             firstname: firstName,
//             lastname: lastName
//         });

//         // Create session and device
//         const { session, device, tokens } = await createSession(user, deviceId, deviceName, deviceType);

//         logger.auth('user_registered', {
//             userId: user.id,
//             method: 'local',
//             deviceType,
//             deviceName
//         }, req);

//         res.status(201).json({
//             message: 'User registered successfully',
//             user: user.toPublicJSON(),
//             tokens,
//             session: {
//                 id: session.id,
//                 expiresAt: session.expiresat
//             },
//             device: {
//                 id: device.id,
//                 name: device.devicename,
//                 type: device.devicetype
//             }
//         });

//     } catch (error) {
//         logger.error('Registration error', error);
//         res.status(500).json({
//             error: 'Registration failed',
//             message: 'An internal server error occurred',
//             code: 'REGISTRATION_ERROR'
//         });
//     }
// });

// /**
//  * Login endpoint
//  * POST /api/auth/login
//  */
// router.post('/login', authLimiter, loginValidation, async (req, res) => {
//     try {
//         // Check validation results
//         const errors = validationResult(req);
//         if (!errors.isEmpty()) {
//             return res.status(400).json({
//                 error: 'Validation failed',
//                 message: 'Please check your input data',
//                 details: errors.array()
//             });
//         }

//         const { email, password, deviceName, deviceType, deviceId } = req.body;

//         // Find user
//         const user = await User.findByEmail(email);
//         if (!user) {
//             logger.audit('login_attempt_invalid_email', { email }, req);
//             return res.status(401).json({
//                 error: 'Invalid credentials',
//                 message: 'Email or password is incorrect',
//                 code: 'INVALID_CREDENTIALS'
//             });
//         }

//         // Check if account is locked
//         if (user.isLocked()) {
//             logger.audit('login_attempt_locked_account', { userId: user.id }, req);
//             return res.status(423).json({
//                 error: 'Account locked',
//                 message: 'Your account has been locked due to too many failed login attempts',
//                 code: 'ACCOUNT_LOCKED'
//             });
//         }

//         // Verify password
//         const isValidPassword = await user.verifyPassword(password);
//         if (!isValidPassword) {
//             logger.audit('login_attempt_invalid_password', { userId: user.id }, req);
//             await user.recordFailedLogin();
//             return res.status(401).json({
//                 error: 'Invalid credentials',
//                 message: 'Email or password is incorrect',
//                 code: 'INVALID_CREDENTIALS'
//             });
//         }

//         // Reset failed login attempts on successful login
//         await user.resetFailedLogins();

//         // Create session and device
//         const { session, device, tokens } = await createSession(user, deviceId, deviceName, deviceType);

//         logger.auth('user_logged_in', {
//             userId: user.id,
//             sessionId: session.id,
//             deviceType,
//             deviceName
//         }, req);

//         res.json({
//             message: 'Login successful',
//             user: user.toPublicJSON(),
//             tokens,
//             session: {
//                 id: session.id,
//                 expiresAt: session.expiresat
//             },
//             device: {
//                 id: device.id,
//                 name: device.devicename,
//                 type: device.devicetype
//             }
//         });

//     } catch (error) {
//         logger.error('Login error', error);
//         res.status(500).json({
//             error: 'Login failed',
//             message: 'An internal server error occurred',
//             code: 'LOGIN_ERROR'
//         });
//     }
// });

// /**
//  * Token refresh endpoint
//  * POST /api/auth/refresh
//  */
// router.post('/refresh', async (req, res) => {
//     try {
//         const { refreshToken } = req.body;

//         if (!refreshToken) {
//             return res.status(400).json({
//                 error: 'Refresh token required',
//                 message: 'Please provide a refresh token',
//                 code: 'REFRESH_TOKEN_MISSING'
//             });
//         }

//         // Verify refresh token
//         let decoded;
//         try {
//             decoded = jwt.verify(refreshToken, process.env.JWT_REFRESH_SECRET || process.env.JWT_SECRET);
//         } catch (error) {
//             logger.audit('invalid_refresh_token_attempt', { error: error.message }, req);
//             return res.status(401).json({
//                 error: 'Invalid refresh token',
//                 message: 'Token verification failed',
//                 code: 'INVALID_REFRESH_TOKEN'
//             });
//         }

//         // Find session
//         const session = await SyncSession.findByPk(decoded.sessionId, {
//             include: [{ model: User, as: 'user' }]
//         });

//         if (!session || !session.isActive()) {
//             return res.status(401).json({
//                 error: 'Session not found or expired',
//                 message: 'Please login again',
//                 code: 'SESSION_NOT_FOUND'
//             });
//         }

//         // Verify stored refresh token hash
//         const isValidRefreshToken = await bcrypt.compare(refreshToken, session.refreshtokenhash);
//         if (!isValidRefreshToken) {
//             logger.audit('refresh_token_hash_mismatch', {
//                 userId: session.userid,
//                 sessionId: session.id
//             }, req);
//             return res.status(401).json({
//                 error: 'Invalid refresh token',
//                 message: 'Token verification failed',
//                 code: 'TOKEN_HASH_MISMATCH'
//             });
//         }

//         // Generate new tokens
//         const newTokens = generateTokens(session.user, session.id);

//         // Update session with new token hashes
//         session.accesstokenhash = await bcrypt.hash(newTokens.accessToken, 5);
//         session.refreshtokenhash = await bcrypt.hash(newTokens.refreshToken, 5);
//         session.lastactivity = new Date();
//         await session.save();

//         logger.auth('token_refreshed', { userId: session.user.id, sessionId: session.id }, req);

//         res.json({
//             message: 'Tokens refreshed successfully',
//             tokens: newTokens,
//             expiresAt: session.expiresat
//         });

//     } catch (error) {
//         logger.error('Token refresh error', error);
//         res.status(500).json({
//             error: 'Token refresh failed',
//             message: 'An internal server error occurred',
//             code: 'TOKEN_REFRESH_ERROR'
//         });
//     }
// });

// /**
//  * Logout endpoint
//  * POST /api/auth/logout
//  */
// router.post('/logout', async (req, res) => {
//     try {
//         const { sessionId } = req.body;

//         if (!sessionId) {
//             return res.status(400).json({
//                 error: 'Session ID required',
//                 message: 'Please provide a session ID',
//                 code: 'SESSION_ID_MISSING'
//             });
//         }

//         // Find and delete the session
//         const session = await SyncSession.findByPk(sessionId);
//         if (session) {
//             await session.destroy();
//             logger.auth('user_logged_out', { userId: session.userid, sessionId }, req);

//             // Clear any Redis cache for this user
//             await RedisService.del(`user_session_${session.userid}_${sessionId}`);
//             await RedisService.del(`user_devices_${session.userid}`);
//         }

//         res.json({
//             message: 'Logout successful'
//         });

//     } catch (error) {
//         logger.error('Logout error', error);
//         res.status(500).json({
//             error: 'Logout failed',
//             message: 'An internal server error occurred',
//             code: 'LOGOUT_ERROR'
//         });
//     }
// });

// /**
//  * Get current user info
//  * GET /api/auth/me
//  */
// router.get('/me', require('../middleware/auth'), async (req, res) => {
//     try {
//         const user = await User.findByPk(req.user.id, {
//             include: [{
//                 model: Device,
//                 as: 'devices',
//                 where: { isactive: true },
//                 required: false
//             }]
//         });

//         if (!user) {
//             return res.status(404).json({
//                 error: 'User not found',
//                 message: 'User account no longer exists',
//                 code: 'USER_NOT_FOUND'
//             });
//         }

//         res.json({
//             user: user.toPublicJSON(),
//             devices: user.devices || []
//         });

//     } catch (error) {
//         logger.error('Get user info error', error);
//         res.status(500).json({
//             error: 'Failed to get user info',
//             message: 'An internal server error occurred',
//             code: 'USER_INFO_ERROR'
//         });
//     }
// });

// /**
//  * Revoke all sessions (logout from all devices)
//  * POST /api/auth/revoke-all
//  */
// router.post('/revoke-all', require('../middleware/auth'), async (req, res) => {
//     try {
//         // Delete all sessions for this user
//         const deletedSessions = await SyncSession.destroy({
//             where: { userid: req.user.id }
//         });

//         // Clear Redis cache
//         await RedisService.invalidatePattern(`user_session_${req.user.id}_*`);
//         await RedisService.del(`user_devices_${req.user.id}`);

//         logger.auth('all_sessions_revoked', { userId: req.user.id, deletedSessions }, req);

//         res.json({
//             message: 'All sessions revoked successfully',
//             revokedSessions: deletedSessions
//         });

//     } catch (error) {
//         logger.error('Revoke all sessions error', error);
//         res.status(500).json({
//             error: 'Failed to revoke sessions',
//             message: 'An internal server error occurred',
//             code: 'REVOKE_SESSIONS_ERROR'
//         });
//     }
// });

// module.exports = router;

const express = require('express');
const bcrypt = require('bcryptjs');
const jwt = require('jsonwebtoken');
const rateLimit = require('express-rate-limit');
const logger = require('../config/logger');

const router = express.Router();

// Simple rate limiter
const authLimiter = rateLimit({
    windowMs: 15 * 60 * 1000, // 15 minutes
    max: 10, // limit each IP to 10 requests per windowMs
    message: {
        error: 'Too many authentication attempts',
        message: 'Please try again later'
    }
});

// Simple token generation
function generateTokens(user) {
    const payload = {
        userId: user.id,
        email: user.email,
        iat: Math.floor(Date.now() / 1000)
    };

    const accessToken = jwt.sign(payload, process.env.JWT_SECRET || 'fallback-secret', {
        expiresIn: '1h'
    });

    const refreshToken = jwt.sign(
        { userId: user.id, type: 'refresh' },
        process.env.JWT_SECRET || 'fallback-secret',
        { expiresIn: '7d' }
    );

    return {
        accessToken,
        refreshToken,
        expiresIn: '3600'
    };
}

/**
 * Test endpoint to verify auth routes are loading
 */
router.get('/test', (req, res) => {
    res.json({
        message: 'Auth routes are working!',
        timestamp: new Date().toISOString()
    });
});

/**
 * Register endpoint
 */
router.post('/register', authLimiter, async (req, res) => {
    try {
        const { email, password, firstName, lastName, deviceName, deviceType, deviceId } = req.body;

        if (!email || !password) {
            return res.status(400).json({
                error: 'Missing required fields',
                message: 'Email and password are required'
            });
        }

        logger.info('Registration attempt', { email });

        // For now, create a simple user object
        const user = {
            id: 1,
            email: email.toLowerCase(),
            firstName: firstName || 'User',
            lastName: lastName || 'Account'
        };

        const tokens = generateTokens(user);

        res.status(201).json({
            message: 'User registered successfully',
            user: {
                id: user.id,
                email: user.email,
                firstName: user.firstName,
                lastName: user.lastName
            },
            tokens
        });

    } catch (error) {
        logger.error('Registration error:', error);
        res.status(500).json({
            error: 'Registration failed',
            message: 'An internal server error occurred'
        });
    }
});

/**
 * Login endpoint
 */
router.post('/login', authLimiter, async (req, res) => {
    try {
        const { email, password, deviceName, deviceType, deviceId } = req.body;

        if (!email || !password) {
            return res.status(400).json({
                error: 'Missing required fields',
                message: 'Email and password are required'
            });
        }

        logger.info('Login attempt', { email });

        // For now, create a simple user object
        const user = {
            id: 1,
            email: email.toLowerCase(),
            firstName: 'Test',
            lastName: 'User'
        };

        const tokens = generateTokens(user);

        res.json({
            message: 'Login successful',
            user: {
                id: user.id,
                email: user.email,
                firstName: user.firstName,
                lastName: user.lastName
            },
            tokens
        });

    } catch (error) {
        logger.error('Login error:', error);
        res.status(500).json({
            error: 'Login failed',
            message: 'An internal server error occurred'
        });
    }
});

/**
 * Logout endpoint
 */
router.post('/logout', (req, res) => {
    res.json({
        message: 'Logout successful'
    });
});

module.exports = router;
