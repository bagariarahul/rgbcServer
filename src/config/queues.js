const logger = require('./logger');

// Simple in-memory queue implementation for Phase 1
// This will be replaced with Redis-based Bull queues later

class SimpleQueue {
    constructor(name) {
        this.name = name;
        this.jobs = [];
        this.processing = false;
    }

    async add(jobType, data, options = {}) {
        const job = {
            id: require('crypto').randomUUID(),
            type: jobType,
            data,
            options,
            status: 'waiting',
            createdAt: new Date(),
            attempts: 0,
            maxAttempts: options.attempts || 3
        };

        this.jobs.push(job);
        logger.debug(`Job added to ${this.name} queue`, { jobId: job.id, type: jobType });

        // Start processing if not already processing
        if (!this.processing) {
            this.processJobs();
        }

        return job;
    }

    async processJobs() {
        if (this.processing) return;
        this.processing = true;

        while (this.jobs.length > 0) {
            const job = this.jobs.find(j => j.status === 'waiting');
            if (!job) break;

            job.status = 'active';
            job.startedAt = new Date();

            try {
                logger.debug(`Processing job ${job.id} of type ${job.type}`);
                
                // Simulate job processing - in a real implementation, 
                // this would call the appropriate processor function
                await new Promise(resolve => setTimeout(resolve, 100));
                
                job.status = 'completed';
                job.completedAt = new Date();
                
                logger.debug(`Job ${job.id} completed successfully`);
                
                // Remove completed job
                this.jobs = this.jobs.filter(j => j.id !== job.id);

            } catch (error) {
                job.attempts++;
                
                if (job.attempts >= job.maxAttempts) {
                    job.status = 'failed';
                    job.failedAt = new Date();
                    job.error = error.message;
                    logger.error(`Job ${job.id} failed permanently`, { error: error.message });
                } else {
                    job.status = 'waiting';
                    logger.warn(`Job ${job.id} failed, will retry`, { 
                        attempt: job.attempts, 
                        maxAttempts: job.maxAttempts 
                    });
                }
            }
        }

        this.processing = false;
    }

    getStats() {
        return {
            waiting: this.jobs.filter(j => j.status === 'waiting').length,
            active: this.jobs.filter(j => j.status === 'active').length,
            completed: 0, // Completed jobs are removed
            failed: this.jobs.filter(j => j.status === 'failed').length
        };
    }
}

// Create queue instances
const uploadQueue = new SimpleQueue('upload');
const processingQueue = new SimpleQueue('processing');
const previewQueue = new SimpleQueue('preview');
const syncQueue = new SimpleQueue('sync');
const cleanupQueue = new SimpleQueue('cleanup');

// Initialize queues (placeholder for now)
async function initializeQueues() {
    logger.info('Simple queue system initialized');
    return Promise.resolve();
}

// Queue manager
class QueueManager {
    static async addChunkedUploadJob(jobData, options = {}) {
        return await uploadQueue.add('chunked_upload', jobData, options);
    }

    static async addProcessingJob(jobData, options = {}) {
        return await processingQueue.add('process_file', jobData, options);
    }

    static async addPreviewJob(jobData, options = {}) {
        return await previewQueue.add('generate_preview', jobData, options);
    }

    static async addSyncJob(jobData, options = {}) {
        return await syncQueue.add('notify_sync', jobData, options);
    }

    static async getQueueStats() {
        return {
            uploadQueue: uploadQueue.getStats(),
            processingQueue: processingQueue.getStats(),
            previewQueue: previewQueue.getStats(),
            syncQueue: syncQueue.getStats(),
            cleanupQueue: cleanupQueue.getStats()
        };
    }

    static async pauseAllQueues() {
        // Placeholder - simple queues don't support pause/resume
        logger.info('Pause operation not supported in simple queue mode');
        return Promise.resolve();
    }

    static async resumeAllQueues() {
        // Placeholder - simple queues don't support pause/resume
        logger.info('Resume operation not supported in simple queue mode');
        return Promise.resolve();
    }
}

module.exports = {
    uploadQueue,
    processingQueue,
    previewQueue,
    syncQueue,
    cleanupQueue,
    initializeQueues,
    QueueManager
};