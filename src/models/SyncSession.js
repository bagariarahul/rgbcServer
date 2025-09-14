const { DataTypes } = require('sequelize');

module.exports = (sequelize) => {
    const SyncSession = sequelize.define('sync_sessions', {
        id: {
            type: DataTypes.UUID,
            defaultValue: DataTypes.UUIDV4,
            primaryKey: true
        },
        user_id: {
            type: DataTypes.UUID,
            allowNull: false,
            references: {
                model: 'users',
                key: 'id'
            },
            onUpdate: 'CASCADE',
            onDelete: 'CASCADE'
        },
        device_id: {
            type: DataTypes.UUID,
            allowNull: false,
            references: {
                model: 'devices',
                key: 'id'
            },
            onUpdate: 'CASCADE',
            onDelete: 'CASCADE'
        },
        access_token_hash: {
            type: DataTypes.STRING(255),
            allowNull: false
        },
        refresh_token_hash: {
            type: DataTypes.STRING(255),
            allowNull: false
        },
        expires_at: {
            type: DataTypes.DATE,
            allowNull: false
        },
        last_activity: {
            type: DataTypes.DATE,
            defaultValue: DataTypes.NOW
        },
        ip_address: {
            type: DataTypes.INET,
            allowNull: true
        },
        user_agent: {
            type: DataTypes.TEXT,
            allowNull: true
        },
        is_revoked: {
            type: DataTypes.BOOLEAN,
            defaultValue: false
        },
        revoked_at: {
            type: DataTypes.DATE,
            allowNull: true
        },
        revoke_reason: {
            type: DataTypes.STRING(100),
            allowNull: true
        }
    }, {
        indexes: [
            {
                fields: ['user_id']
            },
            {
                fields: ['device_id']
            },
            {
                fields: ['expires_at']
            },
            {
                fields: ['last_activity']
            },
            {
                fields: ['is_revoked']
            }
        ]
    });

    // Instance methods
    SyncSession.prototype.isExpired = function() {
        return new Date() > this.expires_at;
    };

    SyncSession.prototype.isActive = function() {
        return !this.is_revoked && !this.isExpired();
    };

    SyncSession.prototype.revoke = function(reason = 'USER_ACTION') {
        this.is_revoked = true;
        this.revoked_at = new Date();
        this.revoke_reason = reason;
        return this.save();
    };

    SyncSession.prototype.updateActivity = function(ipAddress = null, userAgent = null) {
        this.last_activity = new Date();
        if (ipAddress) this.ip_address = ipAddress;
        if (userAgent) this.user_agent = userAgent;
        return this.save();
    };

    SyncSession.prototype.extend = function(additionalTime = 7 * 24 * 60 * 60 * 1000) {
        this.expires_at = new Date(this.expires_at.getTime() + additionalTime);
        return this.save();
    };

    // Static methods
    SyncSession.findActiveByUserId = function(userId) {
        return this.findAll({
            where: {
                user_id: userId,
                is_revoked: false,
                expires_at: {
                    [sequelize.Sequelize.Op.gt]: new Date()
                }
            },
            include: [{
                model: sequelize.models.devices,
                as: 'device'
            }],
            order: [['last_activity', 'DESC']]
        });
    };

    SyncSession.findByIdAndUserId = function(sessionId, userId) {
        return this.findOne({
            where: {
                id: sessionId,
                user_id: userId,
                is_revoked: false,
                expires_at: {
                    [sequelize.Sequelize.Op.gt]: new Date()
                }
            },
            include: [{
                model: sequelize.models.users,
                as: 'user'
            }, {
                model: sequelize.models.devices,
                as: 'device'
            }]
        });
    };

    SyncSession.revokeAllForUser = async function(userId, reason = 'ADMIN_ACTION') {
        const sessions = await this.findAll({
            where: {
                user_id: userId,
                is_revoked: false
            }
        });

        for (const session of sessions) {
            await session.revoke(reason);
        }

        return sessions.length;
    };

    SyncSession.cleanupExpired = async function() {
        const expiredSessions = await this.destroy({
            where: {
                expires_at: {
                    [sequelize.Sequelize.Op.lt]: new Date()
                }
            }
        });

        return expiredSessions;
    };

    return SyncSession;
};