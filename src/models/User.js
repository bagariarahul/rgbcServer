const { DataTypes } = require('sequelize');
const bcrypt = require('bcryptjs');
const crypto = require('crypto');

module.exports = (sequelize) => {
    const User = sequelize.define('users', {
        id: {
            type: DataTypes.UUID,
            defaultValue: DataTypes.UUIDV4,
            primaryKey: true
        },
        email: {
            type: DataTypes.STRING(255),
            allowNull: false,
            unique: true,
            validate: {
                isEmail: true,
                len: [3, 255]
            }
        },
        password_hash: {
            type: DataTypes.STRING(255),
            allowNull: true // Nullable for OAuth-only users
        },
        first_name: {
            type: DataTypes.STRING(100),
            allowNull: false,
            validate: {
                len: [1, 100],
                notEmpty: true
            }
        },
        last_name: {
            type: DataTypes.STRING(100),
            allowNull: false,
            validate: {
                len: [1, 100],
                notEmpty: true
            }
        },
        avatar_url: {
            type: DataTypes.TEXT,
            allowNull: true
        },
        google_id: {
            type: DataTypes.STRING(50),
            allowNull: true,
            unique: true
        },
        google_refresh_token: {
            type: DataTypes.TEXT,
            allowNull: true
        },
        auth_provider: {
            type: DataTypes.ENUM('LOCAL', 'GOOGLE', 'HYBRID'),
            defaultValue: 'LOCAL',
            allowNull: false
        },
        email_verified: {
            type: DataTypes.BOOLEAN,
            defaultValue: false
        },
        email_verification_token: {
            type: DataTypes.STRING(255),
            allowNull: true
        },
        email_verification_expires: {
            type: DataTypes.DATE,
            allowNull: true
        },
        password_reset_token: {
            type: DataTypes.STRING(255),
            allowNull: true
        },
        password_reset_expires: {
            type: DataTypes.DATE,
            allowNull: true
        },
        storage_quota: {
            type: DataTypes.BIGINT,
            defaultValue: 107374182400, // 100GB default
            allowNull: false
        },
        storage_used: {
            type: DataTypes.BIGINT,
            defaultValue: 0,
            allowNull: false
        },
        subscription_type: {
            type: DataTypes.ENUM('FREE', 'PREMIUM', 'ENTERPRISE'),
            defaultValue: 'FREE'
        },
        subscription_expires: {
            type: DataTypes.DATE,
            allowNull: true
        },
        is_active: {
            type: DataTypes.BOOLEAN,
            defaultValue: true
        },
        is_admin: {
            type: DataTypes.BOOLEAN,
            defaultValue: false
        },
        two_factor_enabled: {
            type: DataTypes.BOOLEAN,
            defaultValue: false
        },
        two_factor_secret: {
            type: DataTypes.STRING(255),
            allowNull: true
        },
        backup_codes: {
            type: DataTypes.JSON,
            allowNull: true,
            defaultValue: []
        },
        login_attempts: {
            type: DataTypes.INTEGER,
            defaultValue: 0
        },
        locked_until: {
            type: DataTypes.DATE,
            allowNull: true
        },
        last_login: {
            type: DataTypes.DATE,
            allowNull: true
        },
        last_activity: {
            type: DataTypes.DATE,
            allowNull: true
        },
        timezone: {
            type: DataTypes.STRING(50),
            defaultValue: 'UTC'
        },
        language: {
            type: DataTypes.STRING(10),
            defaultValue: 'en'
        },
        notification_preferences: {
            type: DataTypes.JSON,
            defaultValue: {
                email: true,
                push: true,
                sync: true,
                security: true
            }
        },
        privacy_settings: {
            type: DataTypes.JSON,
            defaultValue: {
                analytics: true,
                error_reporting: true,
                usage_stats: true
            }
        },
        sync_settings: {
            type: DataTypes.JSON,
            defaultValue: {
                auto_backup: true,
                wifi_only: false,
                backup_photos: true,
                backup_videos: true,
                backup_documents: true,
                chunk_size: 5242880, // 5MB
                parallel_uploads: 3
            }
        }
    }, {
        indexes: [
            {
                unique: true,
                fields: ['email']
            },
            {
                unique: true,
                fields: ['google_id'],
                where: {
                    google_id: {
                        [sequelize.Sequelize.Op.ne]: null
                    }
                }
            },
            {
                fields: ['auth_provider']
            },
            {
                fields: ['is_active']
            },
            {
                fields: ['created_at']
            }
        ],
        hooks: {
            beforeCreate: async (user) => {
                // Hash password if provided
                if (user.password_hash) {
                    const saltRounds = parseInt(process.env.BCRYPT_ROUNDS) || 12;
                    user.password_hash = await bcrypt.hash(user.password_hash, saltRounds);
                }

                // Generate email verification token if email not verified
                if (!user.email_verified && !user.google_id) {
                    user.email_verification_token = crypto.randomBytes(32).toString('hex');
                    user.email_verification_expires = new Date(Date.now() + 24 * 60 * 60 * 1000); // 24 hours
                }

                // Set email as verified for Google OAuth users
                if (user.google_id) {
                    user.email_verified = true;
                }
            },
            beforeUpdate: async (user) => {
                // Hash password if changed
                if (user.changed('password_hash') && user.password_hash) {
                    const saltRounds = parseInt(process.env.BCRYPT_ROUNDS) || 12;
                    user.password_hash = await bcrypt.hash(user.password_hash, saltRounds);
                }
            }
        }
    });

    // Instance methods
    User.prototype.validatePassword = async function(password) {
        if (!this.password_hash) {
            throw new Error('User has no password set (OAuth user)');
        }
        return await bcrypt.compare(password, this.password_hash);
    };

    User.prototype.generateEmailVerificationToken = function() {
        this.email_verification_token = crypto.randomBytes(32).toString('hex');
        this.email_verification_expires = new Date(Date.now() + 24 * 60 * 60 * 1000); // 24 hours
        return this.email_verification_token;
    };

    User.prototype.generatePasswordResetToken = function() {
        this.password_reset_token = crypto.randomBytes(32).toString('hex');
        this.password_reset_expires = new Date(Date.now() + 60 * 60 * 1000); // 1 hour
        return this.password_reset_token;
    };

    User.prototype.clearPasswordReset = function() {
        this.password_reset_token = null;
        this.password_reset_expires = null;
    };

    User.prototype.isEmailVerificationExpired = function() {
        return this.email_verification_expires && this.email_verification_expires < new Date();
    };

    User.prototype.isPasswordResetExpired = function() {
        return this.password_reset_expires && this.password_reset_expires < new Date();
    };

    User.prototype.incrementLoginAttempts = function() {
        this.login_attempts += 1;
        
        // Lock account after 5 failed attempts for 30 minutes
        if (this.login_attempts >= 5) {
            this.locked_until = new Date(Date.now() + 30 * 60 * 1000);
        }
        
        return this.save();
    };

    User.prototype.resetLoginAttempts = function() {
        this.login_attempts = 0;
        this.locked_until = null;
        this.last_login = new Date();
        this.last_activity = new Date();
        return this.save();
    };

    User.prototype.isLocked = function() {
        return this.locked_until && this.locked_until > new Date();
    };

    User.prototype.updateStorageUsed = function(bytes) {
        this.storage_used = Math.max(0, this.storage_used + bytes);
        return this.save();
    };

    User.prototype.getStoragePercentage = function() {
        return Math.round((this.storage_used / this.storage_quota) * 100);
    };

    User.prototype.isStorageQuotaExceeded = function() {
        return this.storage_used >= this.storage_quota;
    };

    User.prototype.getRemainingStorage = function() {
        return Math.max(0, this.storage_quota - this.storage_used);
    };

    User.prototype.toPublicJSON = function() {
        const user = this.toJSON();
        
        // Remove sensitive fields
        delete user.password_hash;
        delete user.google_refresh_token;
        delete user.email_verification_token;
        delete user.password_reset_token;
        delete user.two_factor_secret;
        delete user.backup_codes;
        delete user.login_attempts;
        delete user.locked_until;
        
        return user;
    };

    User.prototype.canAccessFile = function(file) {
        // Admin can access any file
        if (this.is_admin) return true;
        
        // Users can only access their own files
        return file.user_id === this.id;
    };

    // Static methods
    User.findByEmail = function(email) {
        return this.findOne({ 
            where: { 
                email: email.toLowerCase(),
                is_active: true 
            } 
        });
    };

    User.findByGoogleId = function(googleId) {
        return this.findOne({ 
            where: { 
                google_id: googleId,
                is_active: true 
            } 
        });
    };

    User.findByEmailVerificationToken = function(token) {
        return this.findOne({
            where: {
                email_verification_token: token,
                email_verification_expires: {
                    [sequelize.Sequelize.Op.gt]: new Date()
                }
            }
        });
    };

    User.findByPasswordResetToken = function(token) {
        return this.findOne({
            where: {
                password_reset_token: token,
                password_reset_expires: {
                    [sequelize.Sequelize.Op.gt]: new Date()
                }
            }
        });
    };

    User.createGoogleUser = async function(googleProfile, refreshToken = null) {
        const userData = {
            email: googleProfile.emails[0].value.toLowerCase(),
            first_name: googleProfile.name.givenName,
            last_name: googleProfile.name.familyName,
            avatar_url: googleProfile.photos?.[0]?.value,
            google_id: googleProfile.id,
            google_refresh_token: refreshToken,
            auth_provider: 'GOOGLE',
            email_verified: true
        };

        return await this.create(userData);
    };

    User.linkGoogleAccount = async function(userId, googleProfile, refreshToken = null) {
        const user = await this.findByPk(userId);
        if (!user) throw new Error('User not found');

        user.google_id = googleProfile.id;
        user.google_refresh_token = refreshToken;
        user.auth_provider = user.auth_provider === 'LOCAL' ? 'HYBRID' : 'GOOGLE';
        
        if (googleProfile.photos?.[0]?.value && !user.avatar_url) {
            user.avatar_url = googleProfile.photos[0].value;
        }

        return await user.save();
    };

    return User;
};