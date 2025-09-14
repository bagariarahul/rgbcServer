const { DataTypes } = require('sequelize');

module.exports = (sequelize) => {
    const EncryptionKey = sequelize.define('encryption_keys', {
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
        key_type: {
            type: DataTypes.ENUM('DEK', 'KEK', 'MASTER'),
            allowNull: false,
            comment: 'Data Encryption Key, Key Encryption Key, or Master Key'
        },
        encrypted_key: {
            type: DataTypes.TEXT,
            allowNull: false,
            comment: 'Base64 encoded encrypted key'
        },
        key_hash: {
            type: DataTypes.STRING(64),
            allowNull: false,
            comment: 'Hash for key verification'
        },
        algorithm: {
            type: DataTypes.STRING(50),
            allowNull: false,
            defaultValue: 'AES-256-GCM'
        },
        key_size: {
            type: DataTypes.INTEGER,
            allowNull: false,
            defaultValue: 256
        },
        salt: {
            type: DataTypes.STRING(32),
            allowNull: true,
            comment: 'Salt used for key derivation'
        },
        iv: {
            type: DataTypes.STRING(32),
            allowNull: true,
            comment: 'Initialization vector'
        },
        is_active: {
            type: DataTypes.BOOLEAN,
            defaultValue: true
        },
        expires_at: {
            type: DataTypes.DATE,
            allowNull: true,
            comment: 'Key expiration date for rotation'
        },
        rotated_from_key_id: {
            type: DataTypes.UUID,
            allowNull: true,
            references: {
                model: 'encryption_keys',
                key: 'id'
            },
            onUpdate: 'CASCADE',
            onDelete: 'SET NULL',
            comment: 'Previous key this was rotated from'
        }
    }, {
        indexes: [
            {
                fields: ['user_id']
            },
            {
                fields: ['key_type']
            },
            {
                fields: ['is_active']
            },
            {
                fields: ['expires_at']
            },
            {
                unique: true,
                fields: ['user_id', 'key_type', 'is_active'],
                where: {
                    is_active: true
                }
            }
        ]
    });

    // Instance methods
    EncryptionKey.prototype.isExpired = function() {
        return this.expires_at && this.expires_at < new Date();
    };

    EncryptionKey.prototype.needsRotation = function() {
        if (this.isExpired()) return true;
        
        // Rotate keys older than 90 days
        const rotationDate = new Date();
        rotationDate.setDate(rotationDate.getDate() - 90);
        
        return this.created_at < rotationDate;
    };

    EncryptionKey.prototype.deactivate = function() {
        this.is_active = false;
        return this.save();
    };

    // Static methods
    EncryptionKey.getActiveKey = function(userId, keyType = 'DEK') {
        return this.findOne({
            where: {
                user_id: userId,
                key_type: keyType,
                is_active: true
            },
            order: [['created_at', 'DESC']]
        });
    };

    EncryptionKey.getKeysNeedingRotation = function() {
        const rotationDate = new Date();
        rotationDate.setDate(rotationDate.getDate() - 90);
        
        return this.findAll({
            where: {
                is_active: true,
                [sequelize.Sequelize.Op.or]: [
                    {
                        expires_at: {
                            [sequelize.Sequelize.Op.lt]: new Date()
                        }
                    },
                    {
                        created_at: {
                            [sequelize.Sequelize.Op.lt]: rotationDate
                        }
                    }
                ]
            }
        });
    };

    return EncryptionKey;
};