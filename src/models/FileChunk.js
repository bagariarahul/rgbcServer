const { DataTypes } = require('sequelize');

module.exports = (sequelize) => {
    const FileChunk = sequelize.define('file_chunks', {
        id: {
            type: DataTypes.UUID,
            defaultValue: DataTypes.UUIDV4,
            primaryKey: true
        },
        file_id: {
            type: DataTypes.UUID,
            allowNull: false,
            references: {
                model: 'files',
                key: 'id'
            },
            onUpdate: 'CASCADE',
            onDelete: 'CASCADE'
        },
        chunk_number: {
            type: DataTypes.INTEGER,
            allowNull: false,
            validate: {
                min: 0
            }
        },
        chunk_size: {
            type: DataTypes.INTEGER,
            allowNull: false,
            validate: {
                min: 1
            }
        },
        chunk_hash: {
            type: DataTypes.STRING(64),
            allowNull: false,
            comment: 'SHA-256 hash of chunk data'
        },
        storage_location: {
            type: DataTypes.TEXT,
            allowNull: false
        },
        upload_status: {
            type: DataTypes.ENUM('PENDING', 'UPLOADING', 'COMPLETED', 'FAILED'),
            defaultValue: 'PENDING'
        },
        retry_count: {
            type: DataTypes.INTEGER,
            defaultValue: 0
        },
        error_message: {
            type: DataTypes.TEXT,
            allowNull: true
        }
    }, {
        indexes: [
            {
                fields: ['file_id']
            },
            {
                fields: ['upload_status']
            },
            {
                unique: true,
                fields: ['file_id', 'chunk_number']
            }
        ]
    });

    // Instance methods
    FileChunk.prototype.isCompleted = function() {
        return this.upload_status === 'COMPLETED';
    };

    FileChunk.prototype.canRetry = function() {
        return this.upload_status === 'FAILED' && this.retry_count < 5;
    };

    return FileChunk;
};