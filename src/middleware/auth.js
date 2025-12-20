const jwt = require('jsonwebtoken');
const { User, SyncSession, Device } = require('../config/database');
const RedisService = require('../config/redis');
const logger = require('../config/logger');

/**
 * Authentication middleware for protecting API routes
 * Validates JWT tokens and maintains session state
 */
const authMiddleware = async (req, res, next) => {
    try {
        // Extract token from Authorization header
        const authHeader = req.headers.authorization;
        
        if (!authHeader || !authHeader.startsWith('Bearer ')) {
            return res.status(401).json({
                error: 'Authentication required',
                message: 'Missing or invalid authorization header',
                code: 'AUTH_TOKEN_MISSING'
            });
        }

        const token = authHeader.substring(7);

        // Verify JWT token
        let decoded;
        try {
            decoded = jwt.verify(token, process.env.JWT_SECRET);
        } catch (error) {
            logger.audit('invalid_token_attempt', { 
                error: error.message,
                token: token.substring(0, 10) + '...'
            }, req);
            
            return res.status(401).json({
                error: 'Invalid token',
                message: 'Token verification failed',
                code: 'INVALID_TOKEN'
            });
        }

        // Check token structure
        if (!decoded.userId || !decoded.sessionId) {
            logger.audit('malformed_token', { decoded }, req);
            return res.status(401).json({
                error: 'Invalid token',
                message: 'Token is malformed',
                code: 'MALFORMED_TOKEN'
            });
        }

        // Check Redis cache first for performance
        const cacheKey = `user_session:${decoded.userId}:${decoded.sessionId}`;
        let cachedSession = await RedisService.get(cacheKey);
        
        let session;
        if (cachedSession) {
            // Use cached session data but still verify it's active
            session = cachedSession;
            
            // Verify session hasn't been revoked in database (check periodically)
            const shouldVerifyDB = Math.random() < 0.1; // 10% chance to verify with DB
            if (shouldVerifyDB) {
                const dbSession = await SyncSession.findByIdAndUserId(decoded.sessionId, decoded.userId);
                if (!dbSession || !dbSession.isActive()) {
                    // Session revoked, clear cache
                    await RedisService.del(cacheKey);
                    return res.status(401).json({
                        error: 'Session expired',
                        message: 'Session has been revoked or expired',
                        code: 'SESSION_REVOKED'
                    });
                }
            }
        } else {
            // Fetch from database
            const dbSession = await SyncSession.findByIdAndUserId(decoded.sessionId, decoded.userId);
            
            if (!dbSession) {
                logger.audit('session_not_found', { 
                    userId: decoded.userId, 
                    sessionId: decoded.sessionId 
                }, req);
                
                return res.status(401).json({
                    error: 'Session not found',
                    message: 'Session has expired or been revoked',
                    code: 'SESSION_NOT_FOUND'
                });
            }

            if (!dbSession.isActive()) {
                logger.audit('inactive_session_attempt', { 
                    userId: decoded.userId, 
                    sessionId: decoded.sessionId,
                    isRevoked: dbSession.is_revoked,
                    isExpired: dbSession.isExpired()
                }, req);
                
                return res.status(401).json({
                    error: 'Session inactive',
                    message: 'Session has expired or been revoked',
                    code: 'SESSION_INACTIVE'
                });
            }

            // Cache session data
            session = {
                id: dbSession.id,
                user_id: dbSession.user_id,
                device_id: dbSession.device_id,
                user: dbSession.user ? dbSession.user.toPublicJSON() : null,
                device: dbSession.device ? dbSession.device.toPublicJSON() : null,
                expires_at: dbSession.expires_at,
                last_activity: dbSession.last_activity
            };

            // Cache for 5 minutes
            await RedisService.set(cacheKey, session, 300);
        }

        // Verify stored token hash (security measure)
        if (session.verify_token !== false) { // Allow cache to skip verification
            const dbSession = await SyncSession.findByPk(session.id);
            if (dbSession) {
                const isValidToken = await bcrypt.compare(token, dbSession.access_token_hash);
                if (!isValidToken) {
                    logger.audit('token_hash_mismatch', { 
                        userId: decoded.userId, 
                        sessionId: decoded.sessionId 
                    }, req);
                    
                    // Clear cache as it might be compromised
                    await RedisService.del(cacheKey);
                    
                    return res.status(401).json({
                        error: 'Invalid token',
                        message: 'Token verification failed',
                        code: 'TOKEN_HASH_MISMATCH'
                    });
                }
            }
        }

        // Update last activity (throttled to prevent too many DB writes)
        const lastActivityKey = `last_activity:${decoded.sessionId}`;
        const lastUpdate = await RedisService.get(lastActivityKey);
        const now = Date.now();
        
        // Only update every 60 seconds
        if (!lastUpdate || (now - lastUpdate) > 60000) {
            // Update in background, don't wait
            updateSessionActivity(decoded.sessionId, req).catch(error => {
                logger.warn('Failed to update session activity:', error);
            });
            
            // Cache the update time
            await RedisService.set(lastActivityKey, now, 300);
        }

        // Attach user data to request
        req.user = {
            id: session.user_id,
            email: decoded.email,
            sessionId: session.id,
            deviceId: session.device_id,
            ...session.user
        };

        req.device = session.device;
        req.session = {
            id: session.id,
            expires_at: session.expires_at,
            last_activity: session.last_activity
        };

        // Add user context to logger
        req.logContext = {
            userId: req.user.id,
            sessionId: req.session.id,
            deviceId: req.device?.id
        };

        next();

    } catch (error) {
        logger.error('Authentication middleware error:', error);
        
        // Clear potentially corrupted cache
        if (req.body?.sessionId) {
            await RedisService.del(`user_session:*:${req.body.sessionId}`);
        }
        
        return res.status(500).json({
            error: 'Authentication error',
            message: 'An internal server error occurred during authentication',
            code: 'AUTH_INTERNAL_ERROR'
        });
    }
};

/**
 * Background function to update session activity
 */
async function updateSessionActivity(sessionId, req) {
    try {
        const session = await SyncSession.findByPk(sessionId);
        if (session) {
            await session.updateActivity(
                req.ip || req.connection?.remoteAddress,
                req.get('User-Agent')
            );

            // Also update device last seen
            if (session.device_id) {
                const device = await Device.findByPk(session.device_id);
                if (device) {
                    await device.updateLastSeen();
                }
            }
        }
    } catch (error) {
        logger.warn('Failed to update session activity:', error);
    }
};

module.exports = authMiddleware;
