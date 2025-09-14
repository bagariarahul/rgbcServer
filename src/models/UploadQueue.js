const { DataTypes } = require('sequelize');

module.exports = (sequelize) => {
    const UploadQueue = sequelize.define('upload_queue', {
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
        file_id: {
            type: DataTypes.UUID,
            allowNull: true,
            references: {
                model: 'files',
                key: 'id'
            },
            onUpdate: 'CASCADE',
            onDelete: 'CASCADE'
        },
        device_id: {
            type: DataTypes.UUID,
            allowNull: false
        },
        job_type: {
            type: DataTypes.ENUM(
                'CHUNKED_UPLOAD',
                'FILE_PROCESSING',
                'PREVIEW_GENERATION',
                'ENCRYPTION',
                'VIRUS_SCAN',
                'METADATA_EXTRACTION',
                'STORAGE_CLEANUP',
                'SYNC_NOTIFICATION'
            ),
            allowNull: false
        },
        status: {
            type: DataTypes.ENUM(
                'PENDING',
                'IN_PROGRESS',
                'COMPLETED',
                'FAILED',
                'RETRYING',
                'CANCELLED',
                'EXPIRED'
            ),
            defaultValue: 'PENDING'
        },
        priority: {
            type: DataTypes.INTEGER,
            defaultValue: 100,
            validate: {
                min: 1,
                max: 1000
            }
        },
        file_path: {
            type: DataTypes.TEXT,
            allowNull: true
        },
        file_name: {
            type: DataTypes.STRING(255),
            allowNull: true
        },
        file_size: {
            type: DataTypes.BIGINT,
            allowNull: true
        },
        file_hash: {
            type: DataTypes.STRING(64),
            allowNull: true
        },
        mime_type: {
            type: DataTypes.STRING(100),
            allowNull: true
        },
        chunk_info: {
            type: DataTypes.JSON,
            allowNull: true,
            defaultValue: null,
            comment: 'Contains chunk numbers, total chunks, and chunk status'
        },
        job_data: {
            type: DataTypes.JSON,
            allowNull: true,
            defaultValue: null,
            comment: 'Job-specific data and parameters'
        },
        progress: {
            type: DataTypes.INTEGER,
            defaultValue: 0,
            validate: {
                min: 0,
                max: 100
            }
        },
        retry_count: {
            type: DataTypes.INTEGER,
            defaultValue: 0
        },
        max_retries: {
            type: DataTypes.INTEGER,
            defaultValue: 5
        },
        retry_delay: {
            type: DataTypes.INTEGER,
            defaultValue: 60000, // 1 minute in milliseconds
            comment: 'Delay between retries in milliseconds'
        },
        next_retry_at: {
            type: DataTypes.DATE,
            allowNull: true
        },
        started_at: {
            type: DataTypes.DATE,
            allowNull: true
        },
        completed_at: {
            type: DataTypes.DATE,
            allowNull: true
        },
        failed_at: {
            type: DataTypes.DATE,
            allowNull: true
        },
        error_message: {
            type: DataTypes.TEXT,
            allowNull: true
        },
        error_details: {
            type: DataTypes.JSON,
            allowNull: true
        },
        expires_at: {
            type: DataTypes.DATE,
            allowNull: true,
            defaultValue: () => new Date(Date.now() + 7 * 24 * 60 * 60 * 1000) // 7 days default
        },
        worker_id: {
            type: DataTypes.STRING(100),
            allowNull: true,
            comment: 'ID of the worker processing this job'
        }
    }, {
        indexes: [
            {
                fields: ['user_id']
            },
            {
                fields: ['file_id']
            },
            {
                fields: ['device_id']
            },
            {
                fields: ['status']
            },
            {
                fields: ['job_type']
            },
            {
                fields: ['priority', 'created_at']
            },
            {
                fields: ['next_retry_at'],
                where: {
                    status: 'RETRYING'
                }
            },
            {
                fields: ['expires_at']
            },
            {
                fields: ['file_hash'],
                where: {
                    file_hash: {
                        [sequelize.Sequelize.Op.ne]: null
                    }
                }
            }
        ],
        hooks: {
            beforeCreate: (job) => {
                // Set initial retry delay based on job type
                if (!job.retry_delay) {
                    const delays = {
                        'CHUNKED_UPLOAD': 30000,     // 30 seconds
                        'FILE_PROCESSING': 60000,    // 1 minute
                        'PREVIEW_GENERATION': 120000, // 2 minutes
                        'ENCRYPTION': 60000,         // 1 minute
                        'VIRUS_SCAN': 180000,        // 3 minutes
                        'METADATA_EXTRACTION': 60000, // 1 minute
                        'STORAGE_CLEANUP': 300000,   // 5 minutes
                        'SYNC_NOTIFICATION': 15000   // 15 seconds
                    };
                    job.retry_delay = delays[job.job_type] || 60000;
                }

                // Set max retries based on job type
                if (!job.max_retries) {
                    const maxRetries = {
                        'CHUNKED_UPLOAD': 10,
                        'FILE_PROCESSING': 3,
                        'PREVIEW_GENERATION': 3,
                        'ENCRYPTION': 5,
                        'VIRUS_SCAN': 2,
                        'METADATA_EXTRACTION': 3,
                        'STORAGE_CLEANUP': 2,
                        'SYNC_NOTIFICATION': 5
                    };
                    job.max_retries = maxRetries[job.job_type] || 5;
                }

                // Set priority based on job type
                if (job.priority === 100) {
                    const priorities = {
                        'CHUNKED_UPLOAD': 200,
                        'FILE_PROCESSING': 150,
                        'PREVIEW_GENERATION': 100,
                        'ENCRYPTION': 180,
                        'VIRUS_SCAN': 190,
                        'METADATA_EXTRACTION': 120,
                        'STORAGE_CLEANUP': 50,
                        'SYNC_NOTIFICATION': 300
                    };
                    job.priority = priorities[job.job_type] || 100;
                }
            }
        }
    });

    // Instance methods
    UploadQueue.prototype.markInProgress = function(workerId = null) {
        this.status = 'IN_PROGRESS';
        this.started_at = new Date();
        if (workerId) {
            this.worker_id = workerId;
        }
        return this.save();
    };

    UploadQueue.prototype.markCompleted = function() {
        this.status = 'COMPLETED';
        this.completed_at = new Date();
        this.progress = 100;
        this.error_message = null;
        this.error_details = null;
        return this.save();
    };

    UploadQueue.prototype.markFailed = function(error, shouldRetry = true) {
        this.failed_at = new Date();
        this.error_message = error?.message || error;
        
        if (error instanceof Error) {
            this.error_details = {
                name: error.name,
                stack: error.stack,
                code: error.code
            };
        }

        if (shouldRetry && this.retry_count < this.max_retries) {
            this.retry_count += 1;
            this.status = 'RETRYING';
            
            // Exponential backoff: delay * (2 ^ retry_count)
            const backoffDelay = this.retry_delay * Math.pow(2, this.retry_count - 1);
            this.next_retry_at = new Date(Date.now() + backoffDelay);
        } else {
            this.status = 'FAILED';
            this.next_retry_at = null;
        }

        return this.save();
    };

    UploadQueue.prototype.markCancelled = function() {
        this.status = 'CANCELLED';
        this.next_retry_at = null;
        return this.save();
    };

    UploadQueue.prototype.updateProgress = function(progress) {
        this.progress = Math.min(100, Math.max(0, progress));
        return this.save();
    };

    UploadQueue.prototype.canRetry = function() {
        return this.status === 'RETRYING' && 
               this.retry_count < this.max_retries && 
               this.next_retry_at && 
               this.next_retry_at <= new Date();
    };

    UploadQueue.prototype.isExpired = function() {
        return this.expires_at && this.expires_at <= new Date();
    };

    UploadQueue.prototype.getEstimatedTimeRemaining = function() {
        if (this.progress === 0) return null;
        
        const elapsed = this.started_at ? Date.now() - this.started_at.getTime() : 0;
        const rate = this.progress / elapsed;
        const remaining = (100 - this.progress) / rate;
        
        return remaining;
    };

    UploadQueue.prototype.toPublicJSON = function() {
        const job = this.toJSON();
        
        // Remove sensitive internal fields
        delete job.worker_id;
        delete job.error_details;
        
        return job;
    };

    // Static methods
    UploadQueue.findPendingJobs = function(limit = 10) {
        return this.findAll({
            where: {
                status: 'PENDING'
            },
            order: [
                ['priority', 'DESC'],
                ['created_at', 'ASC']
            ],
            limit
        });
    };

    UploadQueue.findRetryableJobs = function(limit = 10) {
        return this.findAll({
            where: {
                status: 'RETRYING',
                next_retry_at: {
                    [sequelize.Sequelize.Op.lte]: new Date()
                }
            },
            order: [
                ['priority', 'DESC'],
                ['next_retry_at', 'ASC']
            ],
            limit
        });
    };

    UploadQueue.findByFileHash = function(fileHash) {
        return this.findAll({
            where: {
                file_hash: fileHash,
                status: ['PENDING', 'IN_PROGRESS', 'RETRYING']
            }
        });
    };

    UploadQueue.findUserJobs = function(userId, status = null, limit = 50) {
        const where = { user_id: userId };
        if (status) {
            where.status = Array.isArray(status) ? status : [status];
        }

        return this.findAll({
            where,
            order: [['created_at', 'DESC']],
            limit
        });
    };

    UploadQueue.cleanupExpiredJobs = async function() {
        const expiredJobs = await this.findAll({
            where: {
                expires_at: {
                    [sequelize.Sequelize.Op.lte]: new Date()
                }
            }
        });

        const deleteCount = await this.destroy({
            where: {
                expires_at: {
                    [sequelize.Sequelize.Op.lte]: new Date()
                }
            }
        });

        return { expiredJobs, deleteCount };
    };

    UploadQueue.getQueueStats = async function() {
        const stats = await this.findAll({
            attributes: [
                'status',
                [sequelize.fn('COUNT', sequelize.col('id')), 'count']
            ],
            group: ['status'],
            raw: true
        });

        const result = {
            total: 0,
            pending: 0,
            in_progress: 0,
            completed: 0,
            failed: 0,
            retrying: 0,
            cancelled: 0,
            expired: 0
        };

        stats.forEach(stat => {
            result[stat.status.toLowerCase()] = parseInt(stat.count);
            result.total += parseInt(stat.count);
        });

        return result;
    };

    return UploadQueue;
};