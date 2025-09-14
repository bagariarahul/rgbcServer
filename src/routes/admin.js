const express = require('express');
const { User, Device, File, SyncSession, UploadQueue, StorageNode } = require('../config/database');
const { QueueManager } = require('../config/queues');
const { RedisService } = require('../config/redis');
const { adminMiddleware } = require('../middleware/auth');
const logger = require('../config/logger');

const router = express.Router();

// Apply admin middleware to all admin routes
router.use(adminMiddleware);

// =============================================================================
// SYSTEM OVERVIEW
// =============================================================================

// Get system dashboard statistics
router.get('/dashboard', async (req, res) => {
    try {
        // Get user statistics
        const userStats = await User.findOne({
            attributes: [
                [User.sequelize.fn('COUNT', User.sequelize.col('id')), 'total_users'],
                [User.sequelize.fn('COUNT', User.sequelize.literal('CASE WHEN is_active = true THEN 1 END')), 'active_users'],
                [User.sequelize.fn('COUNT', User.sequelize.literal('CASE WHEN created_at > NOW() - INTERVAL \'24 hours\' THEN 1 END')), 'new_users_24h'],
                [User.sequelize.fn('SUM', User.sequelize.col('storage_used')), 'total_storage_used'],
                [User.sequelize.fn('SUM', User.sequelize.col('storage_quota')), 'total_storage_quota']
            ],
            raw: true
        });

        // Get file statistics
        const fileStats = await File.findOne({
            attributes: [
                [File.sequelize.fn('COUNT', File.sequelize.col('id')), 'total_files'],
                [File.sequelize.fn('COUNT', File.sequelize.literal('CASE WHEN backup_status = \'COMPLETED\' THEN 1 END')), 'completed_files'],
                [File.sequelize.fn('COUNT', File.sequelize.literal('CASE WHEN backup_status = \'FAILED\' THEN 1 END')), 'failed_files'],
                [File.sequelize.fn('COUNT', File.sequelize.literal('CASE WHEN created_at > NOW() - INTERVAL \'24 hours\' THEN 1 END')), 'uploaded_24h']
            ],
            raw: true
        });

        // Get device statistics
        const deviceStats = await Device.findOne({
            attributes: [
                [Device.sequelize.fn('COUNT', Device.sequelize.col('id')), 'total_devices'],
                [Device.sequelize.fn('COUNT', Device.sequelize.literal('CASE WHEN is_active = true THEN 1 END')), 'active_devices'],
                [Device.sequelize.fn('COUNT', Device.sequelize.literal('CASE WHEN last_seen_at > NOW() - INTERVAL \'5 minutes\' THEN 1 END')), 'online_devices']
            ],
            raw: true
        });

        // Get queue statistics
        const queueStats = await QueueManager.getQueueStats();

        // Get Redis health
        const redisHealth = await RedisService.healthCheck();

        // System metrics
        const systemMetrics = {
            uptime: process.uptime(),
            memory: process.memoryUsage(),
            cpu: process.cpuUsage(),
            version: process.version,
            platform: process.platform
        };

        res.json({
            users: userStats,
            files: fileStats,
            devices: deviceStats,
            queues: queueStats,
            redis: redisHealth,
            system: systemMetrics,
            timestamp: new Date().toISOString()
        });

    } catch (error) {
        logger.error('Admin dashboard error:', error);
        res.status(500).json({
            error: 'Failed to get dashboard data',
            message: 'An internal server error occurred'
        });
    }
});

// =============================================================================
// USER MANAGEMENT
// =============================================================================

// Get all users with pagination
router.get('/users', async (req, res) => {
    try {
        const { limit = 50, offset = 0, search, status, sort = 'created_at', order = 'DESC' } = req.query;

        const where = {};

        if (search) {
            where[User.sequelize.Op.or] = [
                { email: { [User.sequelize.Op.iLike]: `%${search}%` } },
                { first_name: { [User.sequelize.Op.iLike]: `%${search}%` } },
                { last_name: { [User.sequelize.Op.iLike]: `%${search}%` } }
            ];
        }

        if (status === 'active') {
            where.is_active = true;
        } else if (status === 'inactive') {
            where.is_active = false;
        }

        const users = await User.findAndCountAll({
            where,
            attributes: [
                'id', 'email', 'first_name', 'last_name', 'auth_provider',
                'storage_used', 'storage_quota', 'subscription_type',
                'is_active', 'is_admin', 'last_login', 'created_at'
            ],
            limit: parseInt(limit),
            offset: parseInt(offset),
            order: [[sort, order.toUpperCase()]],
            include: [{
                model: Device,
                as: 'devices',
                attributes: ['id', 'device_name', 'device_type', 'last_seen_at'],
                where: { is_active: true },
                required: false
            }]
        });

        res.json({
            users: users.rows,
            total: users.count,
            limit: parseInt(limit),
            offset: parseInt(offset)
        });

    } catch (error) {
        logger.error('Admin get users error:', error);
        res.status(500).json({
            error: 'Failed to get users',
            message: 'An internal server error occurred'
        });
    }
});

// Get specific user details
router.get('/users/:userId', async (req, res) => {
    try {
        const { userId } = req.params;

        const user = await User.findByPk(userId, {
            include: [{
                model: Device,
                as: 'devices',
                where: { is_active: true },
                required: false
            }, {
                model: SyncSession,
                as: 'sessions',
                where: { is_revoked: false },
                required: false,
                limit: 10,
                order: [['last_activity', 'DESC']]
            }]
        });

        if (!user) {
            return res.status(404).json({
                error: 'User not found'
            });
        }

        // Get user file statistics
        const fileStats = await File.findOne({
            where: { user_id: userId },
            attributes: [
                [File.sequelize.fn('COUNT', File.sequelize.col('id')), 'total_files'],
                [File.sequelize.fn('SUM', File.sequelize.col('file_size')), 'total_size'],
                [File.sequelize.fn('COUNT', File.sequelize.literal('CASE WHEN backup_status = \'COMPLETED\' THEN 1 END')), 'completed_files'],
                [File.sequelize.fn('COUNT', File.sequelize.literal('CASE WHEN backup_status = \'FAILED\' THEN 1 END')), 'failed_files']
            ],
            raw: true
        });

        res.json({
            user: user.toPublicJSON(),
            devices: user.devices || [],
            sessions: user.sessions || [],
            fileStats: fileStats || {
                total_files: 0,
                total_size: 0,
                completed_files: 0,
                failed_files: 0
            }
        });

    } catch (error) {
        logger.error('Admin get user details error:', error);
        res.status(500).json({
            error: 'Failed to get user details',
            message: 'An internal server error occurred'
        });
    }
});

// Update user status
router.put('/users/:userId/status', async (req, res) => {
    try {
        const { userId } = req.params;
        const { is_active, is_admin } = req.body;

        const user = await User.findByPk(userId);
        
        if (!user) {
            return res.status(404).json({
                error: 'User not found'
            });
        }

        if (typeof is_active === 'boolean') {
            user.is_active = is_active;
        }

        if (typeof is_admin === 'boolean') {
            user.is_admin = is_admin;
        }

        await user.save();

        logger.audit('admin_user_status_updated', {
            targetUserId: userId,
            changes: { is_active, is_admin }
        }, req);

        res.json({
            message: 'User status updated successfully',
            user: user.toPublicJSON()
        });

    } catch (error) {
        logger.error('Admin update user status error:', error);
        res.status(500).json({
            error: 'Failed to update user status',
            message: 'An internal server error occurred'
        });
    }
});

// =============================================================================
// SYSTEM MANAGEMENT
// =============================================================================

// Get storage nodes information
router.get('/storage', async (req, res) => {
    try {
        const nodes = await StorageNode.findAll({
            order: [['priority', 'DESC'], ['created_at', 'ASC']]
        });

        const statistics = await StorageNode.getStorageStatistics();

        res.json({
            nodes,
            statistics
        });

    } catch (error) {
        logger.error('Admin get storage error:', error);
        res.status(500).json({
            error: 'Failed to get storage information',
            message: 'An internal server error occurred'
        });
    }
});

// Get queue management interface
router.get('/queues', async (req, res) => {
    try {
        const stats = await QueueManager.getQueueStats();
        
        // Get recent failed jobs from upload queue
        const failedJobs = await UploadQueue.findAll({
            where: {
                status: 'FAILED'
            },
            limit: 20,
            order: [['failed_at', 'DESC']],
            include: [{
                model: User,
                as: 'user',
                attributes: ['id', 'email', 'first_name', 'last_name']
            }]
        });

        res.json({
            statistics: stats,
            failedJobs: failedJobs.map(job => job.toPublicJSON())
        });

    } catch (error) {
        logger.error('Admin get queues error:', error);
        res.status(500).json({
            error: 'Failed to get queue information',
            message: 'An internal server error occurred'
        });
    }
});

// Pause/Resume queues
router.post('/queues/:action', async (req, res) => {
    try {
        const { action } = req.params;

        if (action === 'pause') {
            await QueueManager.pauseAllQueues();
            logger.audit('admin_queues_paused', {}, req);
        } else if (action === 'resume') {
            await QueueManager.resumeAllQueues();
            logger.audit('admin_queues_resumed', {}, req);
        } else {
            return res.status(400).json({
                error: 'Invalid action',
                message: 'Action must be either "pause" or "resume"'
            });
        }

        const stats = await QueueManager.getQueueStats();

        res.json({
            message: `Queues ${action}d successfully`,
            statistics: stats
        });

    } catch (error) {
        logger.error('Admin queue action error:', error);
        res.status(500).json({
            error: 'Failed to perform queue action',
            message: 'An internal server error occurred'
        });
    }
});

// Get system logs
router.get('/logs', async (req, res) => {
    try {
        const { level = 'error', limit = 100, offset = 0 } = req.query;
        
        // This is a placeholder - in a real implementation, you would
        // read from log files or a log database
        res.json({
            message: 'Log endpoint placeholder',
            level,
            limit: parseInt(limit),
            offset: parseInt(offset),
            logs: []
        });

    } catch (error) {
        logger.error('Admin get logs error:', error);
        res.status(500).json({
            error: 'Failed to get logs',
            message: 'An internal server error occurred'
        });
    }
});

// Clear Redis cache
router.post('/cache/clear', async (req, res) => {
    try {
        const { pattern } = req.body;
        
        let clearedKeys = 0;
        if (pattern) {
            clearedKeys = await RedisService.invalidatePattern(pattern);
        } else {
            // Clear common cache patterns
            const patterns = [
                'user_session:*',
                'user_devices:*',
                'file_ops:*',
                'last_activity:*'
            ];
            
            for (const p of patterns) {
                clearedKeys += await RedisService.invalidatePattern(p);
            }
        }

        logger.audit('admin_cache_cleared', {
            pattern: pattern || 'all',
            clearedKeys
        }, req);

        res.json({
            message: 'Cache cleared successfully',
            clearedKeys
        });

    } catch (error) {
        logger.error('Admin clear cache error:', error);
        res.status(500).json({
            error: 'Failed to clear cache',
            message: 'An internal server error occurred'
        });
    }
});

module.exports = router;